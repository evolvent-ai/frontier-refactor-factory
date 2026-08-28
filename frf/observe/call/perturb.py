"""How a subject's source is perturbed, per language.

E3 asks whether a probe corpus can tell a right implementation from a wrong one. To ask that it
needs a WRONG subject, made by editing the right one -- and the edit has to be valid in the
subject's own language, or the mutant is killed by the compiler rather than by the probe and the
gate is credited with judgement it never exercised.

THIS IS THE CALL SEAM'S WORK, NOT A SCALE'S. It lived in `scales/module.py`, which meant a scale
carried JavaScript grammar: which arrows may be replaced, that `exports.X = async function(` names
a definition, that a bare `+` matches `i++`. Every new language then meant editing the scale, and
the scale is shared by kernel and package -- so the knowledge was both in the wrong place and in
the way. Here it sits beside `shims/` and `dispatch.py`, the other two places that already know
one language from another, and a scale can ask for a mutant without knowing any grammar at all.
"""
from __future__ import annotations

import re

# How many DIFFERENT perturbations E3 may try before concluding that a subject cannot be
# distinguished by this crude a mutation. More than one because an edit can be real and still
# semantically inert -- see `mutate` -- and one such edit should not decide the verdict for a
# candidate that is otherwise perfectly gradeable.
MUTATION_ATTEMPTS = 4

# What a perturbation looks like, per language family. Deliberately crude: E3 does not need a
# realistic wrong answer, it needs a PROVABLE difference in behaviour, and the check establishes
# that difference by comparing observations rather than by trusting this table.
#
# Applied to the source as text because a compiled subject has nothing else to perturb, and because
# a factory that parsed each language in order to mutate it would have re-acquired exactly the
# per-language knowledge the wire exists to avoid.
# WHAT A PERTURBATION MAY BE, PER LANGUAGE. One shared table used to be applied to all eight, and it
# was written in Python: `len(` -> `id(` names a Python BUILTIN, and `[-1]`, `[1:]`, `sorted(` and
# ` and ` are Python syntax. Applied to Go or C those do not produce a subtly wrong function, they
# produce a file that does not compile -- and a mutant that does not build is caught by the compiler
# rather than by the probe, so the gate credits the probe with judgement it never exercised. Worse,
# the broken build surfaces as an unclassified subject failure and is charged to the CANDIDATE as a
# material fault, so a submission can be refused for a defect in this table.
#
# The arithmetic entries are SPACED (` + `, not `+`) for the same reason. A bare `+` lands inside
# `i++` and a bare `*` lands inside a pointer declaration like `const char *items`, which turned
# three of C's four attempts into syntax errors. Spacing costs a few sites and buys a mutant that
# builds.
#
# Ordering within each table is deliberate: arithmetic first, because it changes a returned value
# without changing control flow, so a subject that only checks "did it raise" cannot see it.

# True in every language here: comparison operators mean the same thing and are spelled the same.
_COMPARISON = (
    (' + ', ' - '),
    (' * ', ' + '),
    ('>=', '>'),
    ('<=', '<'),
    ('==', '!='),
    (' < ', ' > '),
    (' > ', ' < '),
)

# C-family boolean and the library calls that survive a rename in each language.
_C_FAMILY = _COMPARISON + (
    (' && ', ' || '),
)

_PERTURBATIONS_BY_LANGUAGE = {
    "python": _COMPARISON + (
        (' and ', ' or '),
        ('[-1]', '[0]'),
        ('[0]', '[-1]'),
        ('[1:]', '[:-1]'),
        ('.sort()', '.reverse()'),
        ('sorted(', 'reversed('),
        ('min(', 'max('),
        ('max(', 'min('),
        ('len(', 'id('),          # `id` is a Python builtin, so this still runs
    ),
    "javascript": _C_FAMILY + (
        ('[0]', '[1]'),
        ('.sort(', '.reverse('),
        ('Math.min(', 'Math.max('),
        ('Math.max(', 'Math.min('),
        ('.length', '.length - 1'),
    ),
    "go": _C_FAMILY + (
        ('[0]', '[1]'),
        ('len(', 'cap('),         # both are Go builtins on slices, so this compiles
    ),
    "rust": _C_FAMILY + (
        ('.iter()', '.iter().rev()'),
        ('.min()', '.max()'),
        ('.max()', '.min()'),
        # NOT `.len()` -> `.capacity()`: capacity is on Vec and String but not on a slice, so it
        # fails to compile for exactly the inputs a refactor task is most likely to take.
    ),
    "c": _C_FAMILY,
    "cpp": _C_FAMILY + (
        ('.begin()', '.end()'),
        ('std::min(', 'std::max('),
        ('std::max(', 'std::min('),
    ),
    "java": _C_FAMILY + (
        ('Math.min(', 'Math.max('),
        ('Math.max(', 'Math.min('),
        ('.size()', '.size() - 1'),
    ),
    "ruby": _COMPARISON + (
        (' and ', ' or '),
        (' && ', ' || '),
        ('[-1]', '[0]'),
        ('[0]', '[-1]'),
        # Method swaps carry a trailing delimiter so they cannot land inside a LONGER name:
        # bare `.sort` also matches `.sort_by`, and `.reverse_by` raises NoMethodError -- a mutant
        # killed by the runtime rather than by the probe, which is what this table exists to avoid.
        ('.sort!', '.reverse!'),
        ('.sort(', '.reverse('),
        ('.min(', '.max('),
        ('.max(', '.min('),
    ),
}

# Spellings the pipeline may hand us for the same language.
_LANGUAGE_ALIASES = {
    "py": "python", "js": "javascript", "ts": "javascript", "typescript": "javascript",
    "c++": "cpp", "cc": "cpp", "golang": "go", "rs": "rust", "rb": "ruby",
}


def _perturbations_for(language: str) -> tuple:
    """The substitutions that still COMPILE in `language`.

    An unknown language falls back to the comparison operators, which are spelled the same in every
    language this factory serves. That is a smaller set than a Python-specific one, and a small set
    of valid mutants is worth more than a large set of broken ones: `channels_bite` already reports
    INCONCLUSIVE when nothing can be perturbed, which is an honest answer, whereas a mutant that
    does not build is charged to the candidate.
    """
    key = language.lower()
    key = _LANGUAGE_ALIASES.get(key, key)
    return _PERTURBATIONS_BY_LANGUAGE.get(key, _COMPARISON)


# Kept as the Python table so that anything still reaching for the old name gets a working set
# rather than silence. New code should call `_perturbations_for`.
_PERTURBATIONS = _PERTURBATIONS_BY_LANGUAGE["python"]


def mutate(source: str, language: str, symbol: str = "", attempt: int = 0) -> str:
    """One small change to a subject's source, chosen so the result still compiles.

    Returns the source unchanged when nothing matched, which is not a failure: the mutant then
    behaves identically, the comparison finds no divergence, and E3 reports INCONCLUSIVE. That is
    the honest outcome for a subject nobody could perturb this way, and it is why the check asks
    whether the observation MOVED rather than inferring blindness from a score.

    THE CHANGE MUST LAND IN THE FUNCTION BEING GRADED, which `symbol` is for. Perturbing the first
    `+` in the FILE was the obvious implementation and it is wrong on real material: a mined module
    holds a dozen functions, only one of them is the subject, and the first `+` is almost always in
    a different one -- or in an import, or a docstring. The mutant then behaves identically, E3
    reports INCONCLUSIVE, and the candidate is refused for a defect in this function rather than in
    the material. Measured on a real batch, that was two refusals in three.

    Located by text rather than by parsing, because a factory that parsed each language in order to
    mutate it would have re-acquired exactly the per-language knowledge the wire exists to avoid.
    The window is from the symbol's definition to the next line that starts in the first column,
    which is where a function ends in every language whose blocks are indented, and is a harmless
    over-approximation in the braced ones.
    """
    start, end = _window_of(source, symbol)
    if language.lower() in ("python", "py") and attempt == 0 and symbol:
        newline = source.find("\n", start)
        if newline != -1:
            indent = "    "
            return source[:newline + 1] + indent + "return None\n" + source[newline + 1:]
    # PAST THE SIGNATURE. A perturbation that lands on the definition line renames the function --
    # `min(` -> `max(` turns `find_min_max` into `find_min_min` -- and the shim then cannot find the
    # symbol it was told to serve. The mutant dies on import, which is not a difference in
    # behaviour but a broken build, and it arrives as an unclassified failure counted as OURS.
    signature_end = source.find("\n", start)
    # An expression-bodied arrow's implementation is on the signature line; advancing past it
    # would discard the only mutation site. Block-bodied declarations still start their body on
    # the following line or use the generic operator sites below.
    arrow_on_line = signature_end != -1 and source.find("=>", start, signature_end) != -1
    block_on_line = signature_end != -1 and source.find("{", start, signature_end) != -1
    if signature_end != -1 and not arrow_on_line and not block_on_line:
        start = signature_end + 1
    # EVERY PLACE A PERTURBATION COULD LAND, in a stable order, so that `attempt` selects among
    # them. Enumerating the sites rather than the RULES is what makes the retry work: a subject
    # containing one `[0]` and six `+` offers seven distinct mutants, where counting rules would
    # have offered two.
    sites = []
    if language.lower() in ("python", "py"):
        at = source.find("return ", start, end)
        while at != -1:
            sites.append((at, "return ", "return None # "))
            at = source.find("return ", at + 1, end)
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        # JS/TS modules commonly use expression-bodied exports, so the generic operator table can
        # miss the subject entirely. Replacing a subject return expression is a compiling semantic
        # mutant; changing `.map(` to `.filter(` is a second independent perturbation for array
        # workloads when no return keyword exists in the selected window.
        at = source.find("return ", start, end)
        while at != -1:
            sites.append((at, "return ", "return null /* mutant */; "))
            at = source.find("return ", at + 1, end)
        at = source.find(".map(", start, end)
        while at != -1:
            sites.append((at, ".map(", ".filter("))
            at = source.find(".map(", at + 1, end)
        # Expression-bodied arrows have no `return` token to perturb. Replace only the arrow
        # operator's body on its line; this preserves the declaration and produces a valid module
        # while changing the selected function's value. A block-bodied arrow is excluded because
        # its body is handled by the return/operator sites above.
        #
        # ONLY A TOP-LEVEL ARROW, not one inside a call's argument list. `items.reduce((a, b) =>
        # a + b, 0)` contains `=> a + b`, but the `, 0)` that follows is reduce's SECOND argument and
        # is not part of the arrow at all -- replacing the whole `=> a + b, 0)` swallowed it and
        # left `reduce((a, b) => null /* mutant */;` unclosed, a syntax error that killed the
        # subject in E3 and surfaced as `Node.js v22.23.2`. An arrow in an argument list is preceded
        # by a closing paren (`(a, b) =>` ends with `)` before the `=>`); a top-level arrow is
        # preceded by a parameter name or `)` of a DEFINING paren. Test the nearest non-space char:
        # `)` preceded by a `(` that is itself the parameter close belongs to the argument.
        for match in re.finditer(r"=>\s*(?!\{)([^\n;]+)", source[start:end]):
            at = start + match.start()
            # ONLY A TOP-LEVEL ARROW, not a call back-arrow. `items.reduce((a, b) => a + b, 0)`
            # has a `=>` inside the argument list: replacing `=> a + b, 0)` would swallow reduce's
            # second argument and leave the call unclosed (`(a, b) => null /* mutant */;`), a
            # syntax error that killed the subject in E3 and surfaced as `Node.js v22.23.2`.
            # An INSIDE arrow is preceded by an unmatched `(` (the parameter list of reduce); a
            # top-level one is not. Scan back to the line start for paren balance.
            depth = 0
            for probe in range(at - 1, start - 1, -1):
                char = source[probe]
                if char == ")":
                    depth += 1
                elif char == "(":
                    depth -= 1
                    if depth < 0:
                        break
            if depth < 0:
                break                               # inside an unclosed call: not a target
            sites.append((at, match.group(0), "=> null /* mutant */"))
        # A branch-local return can be unreachable for the generated probes. For block-bodied
        # functions, an entry throw is a deterministic, always-observable fallback and remains
        # valid JavaScript/TypeScript. It is appended after semantic edits so ordinary mutations
        # retain their stronger signal when available.
        opening = source.find("{", start, end)
        if opening != -1:
            sites.append((opening + 1, "", " throw new Error('frf mutant'); "))
    for original, replacement in _perturbations_for(language):
        at = source.find(original, start, end)
        while at != -1:
            sites.append((at, original, replacement))
            at = source.find(original, at + 1, end)
    # Some valid functions (for example a pure membership predicate) contain none of the
    # expression operators above.  For Python, replacing a return expression with ``None`` is a
    # deliberately crude but always-compiling semantic mutation and gives the evidence check an
    # actual changed observation to test.
    # By position, so the first attempt is the earliest edit and the order does not depend on how
    # the table happens to be written.
    # Prefer language-specific semantic edits. Generic operator positions are useful fallback, but
    # sorting them ahead can spend all mutation attempts on inert helpers in JS modules.
    if language.lower() not in ("javascript", "typescript", "js", "ts"):
        sites.sort()
    if attempt >= len(sites):
        return source
    index, original, replacement = sites[attempt]
    # One occurrence, not all of them. Replacing every `+` in a file tends to produce something that
    # does not compile, and a mutant that does not build demonstrates nothing.
    return source[:index] + replacement + source[index + len(original):]


def _window_of(source: str, symbol: str) -> tuple:
    """Where in the file the named function lives. -> (start, end) offsets over the whole file.

    Falls back to the whole file when the symbol cannot be found, which keeps a subject written for
    this factory -- one function, called `entry` -- working exactly as before.
    """
    if not symbol:
        return (0, len(source))
    for marker in ("def %s(" % symbol, "func %s(" % symbol, "fn %s(" % symbol,
                   "function %s(" % symbol, "%s(" % symbol,
                   "%s = (" % symbol, "%s = async (" % symbol,
                   # The package dispatcher is `exports.entry = async function(...)` or
                   # `module.exports.entry = ...`, which none of the above markers match -- so a
                   # package candidate fell back to the WHOLE FILE and the mutation table could
                   # write into `const DISPATCH` at the top level, producing a syntax error that
                   # killed the subject in E3.
                   "exports.%s = (" % symbol, "exports.%s = async (" % symbol,
                   "exports.%s = function(" % symbol, "exports.%s = async function(" % symbol,
                   "module.exports.%s = (" % symbol, "module.exports.%s = async (" % symbol,
                   "module.exports.%s = function(" % symbol,
                   "module.exports.%s = async function(" % symbol,
                   "%s = async (" % symbol):
        opened = source.find(marker)
        if opened == -1:
            continue
        body = source.find("\n", opened)
        if body == -1:
            return (opened, len(source))
        # The end of the definition: the next line that begins in the first column. A blank line
        # does not end it, and neither does a decorator or a comment at the same indent.
        closed = len(source)
        offset = body + 1
        while offset < len(source):
            newline = source.find("\n", offset)
            line = source[offset:newline if newline != -1 else len(source)]
            if line.strip() and not line[:1].isspace() and not line.startswith(("}", ")")):
                closed = offset
                break
            if newline == -1:
                break
            offset = newline + 1
        return (opened, closed)
    return (0, len(source))
