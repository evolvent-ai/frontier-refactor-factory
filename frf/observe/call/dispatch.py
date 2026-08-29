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

# What a mutant returns instead of the real answer: attempt 0 is a missing value, attempt 1 a
# falsy-but-present one. Both are plausible-looking WRONG ANSWERS rather than crashes, because a
# mutant that throws is detected by the wire rather than by the probe's judgement, and would score
# the gate far too generously.
#
# KEYED BY LANGUAGE, because "nothing" is spelled differently in each. This was a two-column tuple
# chosen by `if language in ("javascript", "typescript")`, so Ruby -- which is neither -- was handed
# Python's `None` and the mutant died with `NameError: uninitialized constant None`: a crash scored
# as a detection, which is exactly the failure the paragraph above describes. A dict cannot have that
# bug, and a missing entry is a KeyError at generation time rather than a silently mis-spelled mutant.
_WRONG = {
    "python": ("None", "0"),
    "javascript": ("null", "0"),
    "typescript": ("null", "0"),
    "ruby": ("nil", "0"),
}

def languages() -> tuple:
    """Which languages this module can generate a dispatcher for.

    DERIVED FROM THE GENERATORS, because a hand-written list beside them is a second place to forget
    one -- and that is not hypothetical: a `DYNAMIC` tuple used to live here carrying exactly this
    meaning, the ruby generator was added, and the tuple was not updated. Its own comment warned
    against itself ("a second list of what is supported is a second place to forget one").
    """
    return tuple(sorted(_GENERATORS))


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


def _ruby(dispatch: dict, wrong: str | None) -> str:
    if wrong is not None:
        return ("def entry(op, *args)\n"
                "  %s\n"
                "end\n" % wrong)
    # A DOTTED MODULE BECOMES A PATH. The adapter names an operation's module the way Python spells
    # one -- `pkg.lib.sorting` -- because that is the shape the contract carries for every language.
    # Ruby has no such namespace: `require_relative` takes a path, and `Observer.build` has copied the
    # package to `<package_name>/...` beside this file, so the dots are separators.
    #
    # `require_relative` INSIDE the method, not at the top: a package holds files that raise on load
    # for their own reasons, and requiring all of them up front would fail every operation because one
    # of them is unloadable. Ruby caches a require, so the cost is paid once per operation.
    #
    # EVERY GEM SURFACE IS AN INSTANCE METHOD, which is the one real difference from the old top-level
    # `def` convention. `send` alone can reach only a method on the main object; `MightyString::String
    # #pop` needs an instance of that class first. So when the entry carries a `klass` -- the class
    # that owns the method -- the dispatcher resolves it with `const_get` and calls `Klass.new` before
    # the send. The adapter only records a class with a no-argument `initialize`, so `new` takes no
    # arguments and every probe argument belongs to the method itself.
    #
    # A STATIC method (`def self.x`) carries the same `klass` and would fail on `new`, because Ruby
    # disallows calling `new` on a module that has none... static methods are reached as
    # `klass.method(*args)` without a `.new`; the adapter distinguishes the two through the symbol
    # prefix `self.`.
    # ONLY A REAL CLASS GOES IN THE TABLE. The first cut wrote `(module, symbol, klass or "")`, so an
    # operation with no owning class -- a top-level `def` -- put an EMPTY STRING in the table, and
    # Ruby's truthiness made it a klass: `Object.const_get("")` raised before `send` was ever reached,
    # and every probe of a top-level method failed with a confusing const error. Absent is the only
    # representation that means absent, so an unclassed entry stays a two-element array and the
    # dispatcher's `path, symbol, klass = ...` yields nil for klass, which the `if klass` then
    # correctly reads as "no instance to build".
    # LOAD PATH, ONCE, SO GEM-INTERNAL `require` WORKS. A real gem's entry file says
    # `require "human_time/version"` -- a load-path require, not a relative one. The dispatcher's own
    # `require_relative` finds the entry file itself, but the file's internal requires look in
    # `$LOAD_PATH`, and the room has no path set. Adding these up front is what a gem's own loader does
    # (`lib = File.expand_path('../lib', __FILE__)`), and costing it per call would make every probe a
    # path search; `require` is cached in Ruby, so the guard is measured safe.
    entries = ",\n  ".join(
        "%s => [%s, %s%s]" % (json.dumps(name), json.dumps(module.replace(".", "/")),
                              json.dumps(symbol),
                              (", %s" % json.dumps(klass[0])) if klass else "")
        for name, (module, symbol, *klass) in sorted(dispatch.items()))
    return ("DISPATCH = {\n  %s\n}\n"
            "\n"
            "lib = File.expand_path('lib', __dir__)\n"
            "$LOAD_PATH.unshift(lib) unless $LOAD_PATH.include?(lib)\n"
            "def entry(op, *args)\n"
            "  unless DISPATCH.key?(op)\n"
            "    raise ArgumentError, \"unknown operation: #{op}\"\n"
            "  end\n"
            "  path, symbol, klass = DISPATCH[op]\n"
            "  require_relative path\n"
            "  if klass\n"
            "    owner = Object.const_get(klass)\n"
            "    if symbol.start_with?('self.')\n"
            "      return owner.send(symbol.sub(/self\\./, '').to_sym, *args)\n"
            "    end\n"
            "    # The adapter records only classes whose `initialize` takes no arguments, so `new`\n"
            "    # receives none and every probe argument is the method's own.\n"
            "    return owner.new.send(symbol.to_sym, *args)\n"
            "  end\n"
            "  send(symbol.to_sym, *args)\n"
            "end\n" % entries)


_GENERATORS = {
    "python": _python,
    "javascript": _javascript,
    "typescript": _javascript,
    # Ruby needs no types for this, for the same reason it needed no call bridge: the runtime resolves
    # a name. The four static languages still refuse below -- their dispatchers need a concrete type
    # per argument, and `source/package_adapters.py` emits a signature as a regex-captured STRING
    # rather than typed parameters, so there is nothing yet to generate from.
    "ruby": _ruby,
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
        # `len(attempts)`, not `len(_WRONG)`: the table is keyed by language now, so its length is a
        # count of languages and using it here would cycle through attempts that do not exist.
        attempts = _WRONG[language]
        wrong = attempts[mutant % len(attempts)]
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
