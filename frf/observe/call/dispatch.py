"""The package scale's dispatcher, one small generator per language.

A package task hands the probe a single `entry(op, *args)` seam that fans out to
many public symbols. Writing that seam is per-language work: Python and
JavaScript can look a module up by name at runtime, so their dispatchers are a
table plus a dynamic import. Go, Rust, C, C++ and Java cannot -- a dispatcher
there has to be generated static imports plus a switch, and every argument needs
a concrete type. That is real work, not a missing template, so those languages
refuse loudly here instead of being handed source in the wrong language.

The refusal matters more than it looks. The scale used to decide with
`native = language in ("javascript", "typescript")`, which quietly sent the
other six down the Python branch: a Rust task would get Python source copied to
`subject.rs`. A generator that raises turns that silent mismatch into a stated
gap, which is the only form a gap can be argued with.

Mutants live here too, for the same reason. The scale used to build one by
appending Python source to whatever the subject file was, so a JavaScript mutant
was a syntax error rather than a subtly wrong implementation -- the probe
"caught" it by failing to parse it, which proves nothing about the probe. A
mutant is generated, not concatenated, so it is always a loadable program in the
subject's own language that returns the wrong answer.
"""

from __future__ import annotations

import json

# What a mutant returns instead of the real answer, indexed by attempt. Both are
# plausible-looking wrong answers rather than crashes: a mutant that throws is
# detected by the wire, not by the probe's judgement, and would score the gate
# far too generously.
_WRONG = (
    ("None", "null"),   # attempt 0: a missing value
    ("0", "0"),         # attempt 1: a falsy-but-present value
)

# Languages whose runtime can resolve a module by name, and so can carry the table-driven
# dispatcher. The rest need generated static dispatch.
#
# One source of truth: the same entry drives `supported()`, so a newly added generator is
# automatically a newly supported language. Do NOT maintain a separate tuple here -- a second list
# of what is supported is a second place to forget one.
DYNAMIC = ("python", "javascript", "typescript")


class Unsupported(RuntimeError):
    """Raised when a language has no package-scale dispatcher yet."""


def _python(dispatch: dict, wrong: str | None) -> str:
    if wrong is not None:
        return (
            "def entry(op, *args):\n"
            "    return %s\n" % wrong
        )
    return (
        "import importlib\n"
        "\n"
        "_DISPATCH = %r\n"
        "\n"
        "\n"
        "def entry(op, *args):\n"
        "    if op not in _DISPATCH:\n"
        "        raise ValueError(\"unknown operation: %%s\" %% op)\n"
        "    module_name, symbol = _DISPATCH[op]\n"
        "    return getattr(importlib.import_module(module_name), symbol)(*args)\n"
        % (dispatch,)
    )


def _javascript(dispatch: dict, wrong: str | None) -> str:
    if wrong is not None:
        return (
            "exports.entry = async function(op, ...args) {\n"
            "  return %s;\n"
            "};\n" % wrong
        )
    return (
        "const DISPATCH = %s;\n"
        "exports.entry = async function(op, ...args) {\n"
        "  if (!DISPATCH[op]) throw new Error('unknown operation: ' + op);\n"
        "  const [mod, symbol] = DISPATCH[op];\n"
        "  let loaded;\n"
        "  // require, not dynamic import: require resolves .ts (Node 22 strips types) and .js\n"
        "  // alike, while ESM dynamic import of a .ts path raises a terminal ERR_MODULE_NOT_FOUND\n"
        "  // that the catch cannot recover from. ESM-only subjects keep their type: module .mjs\n"
        "  // or \"type\": \"module\" declares it.\n"
        "  loaded = require(mod);\n"
        "  const fn = loaded[symbol] || (loaded.default && loaded.default[symbol])"
        " || loaded.default;\n"
        "  if (typeof fn !== 'function') throw new Error('export is not callable: '"
        " + symbol);\n"
        "  return fn(...args);\n"
        "};\n" % json.dumps(dispatch)
    )


_GENERATORS = {
    "python": _python,
    "javascript": _javascript,
    "typescript": _javascript,
}


def source(language: str, dispatch: dict, *, mutant: int | None = None) -> str:
    """The dispatcher source for `language`, or a wrong-answer mutant of it.

    `mutant` is the attempt index: the returned program loads and answers every
    operation, but with a wrong value, which is what the mutation gate needs in
    order to say something about the probe rather than about the parser.
    """
    generator = _GENERATORS.get(language)
    if generator is None:
        raise Unsupported(
            "no package-scale dispatcher for %s: dynamic dispatch is unavailable, "
            "so this language needs generated static dispatch with concrete "
            "argument types (supported today: %s)"
            % (language, ", ".join(sorted(_GENERATORS)))
        )
    wrong = None
    if mutant is not None:
        column = 1 if language in ("javascript", "typescript") else 0
        wrong = _WRONG[mutant % len(_WRONG)][column]
    output = generator(dispatch, wrong)
    if language == "typescript":
        # The dispatcher is GENERATED COMMONJS (exports/require). When it is written to a .ts file
        # and passed to tsc, the compiler refuses `exports` and `require` as undeclared names --
        # no @types/node in the offline sandbox. The declarations below are stripped by tsc and
        # cost nothing at runtime, but they let the compiler see what this generated module means.
        output = ("declare var exports: any;\n"
                  "declare function require(mod: string): any;\n" + output)
    return output


def supported(language: str) -> bool:
    """Whether the package scale can serve `language` at all."""
    return language in _GENERATORS
