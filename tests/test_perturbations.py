"""Per-language mutation tables: what may be substituted, and what must never be.

`mutate` promises in its own docstring that its edits are "chosen so the result still compiles",
because a mutant that does not build is killed by the COMPILER rather than by the probe -- and the
gate then credits the probe with judgement it never exercised. One shared table used to be applied to
all eight languages, and it was written in Python, so the promise held for Python only:

  * `len(` -> `id(` names a Python BUILTIN. In Go, Rust, C, C++ and Java it is an undefined
    identifier, so three of C's four attempts were syntax errors.
  * bare `+` and `*` are SUBSTRINGS. `+` lands inside `i++` and `*` lands inside a pointer
    declaration like `const char *items`, which breaks the build without touching any expression.

The consequence was worse than a weak gate. A mutant that will not build surfaces as an unclassified
subject failure, which the pipeline charges to the CANDIDATE as a material fault -- so a perfectly
good submission could be refused for a defect in this table. These tests pin the table to edits that
build.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.call import shims                                          # noqa: E402
from frf.scales import module                                              # noqa: E402

# Spellings that belong to exactly one language, and the languages they would break. Each is a real
# regression: every one of these was reachable from the shared table.
FOREIGN = {
    "id(": ("go", "rust", "c", "cpp", "java"),          # a Python builtin
    "sorted(": ("go", "rust", "c", "cpp", "java"),      # a Python builtin
    "reversed(": ("go", "rust", "c", "cpp", "java"),    # a Python builtin
    " and ": ("go", "rust", "c", "cpp", "java"),        # Python/Ruby boolean spelling
    " or ": ("go", "rust", "c", "cpp", "java"),
    "[-1]": ("go", "c", "cpp", "java"),                 # negative indexing is not legal there
    "[1:]": ("go", "c", "cpp", "java"),                 # Python slice syntax
    ".sort()": ("go", "c", "rust"),                     # a Python list method
}


def test_no_language_table_borrows_another_language_syntax():
    """The exact regression: Go was handed `id(`, a Python builtin, and could not compile."""
    for spelling, broken_in in FOREIGN.items():
        for language in broken_in:
            table = module._perturbations_for(language)
            for original, replacement in table:
                assert spelling not in (original, replacement), (
                    "%s's mutation table contains %r, which does not exist in %s"
                    % (language, spelling, language))


def test_arithmetic_substitutions_cannot_land_inside_an_operator_or_a_pointer():
    """Bare `+` matched `i++` and bare `*` matched `const char *items`.

    Requiring the spaced form costs a handful of mutation sites and buys a mutant that builds, which
    is the only kind the gate can learn anything from.
    """
    for language in sorted(module._PERTURBATIONS_BY_LANGUAGE):
        for original, replacement in module._perturbations_for(language):
            if original.strip() in ("+", "*", "-", "/"):
                assert original.startswith(" ") and original.endswith(" "), (
                    "%s: %r is a bare arithmetic operator and will match inside `i++` or a "
                    "pointer declaration" % (language, original))


def test_every_servable_language_has_perturbations():
    """A language with an empty table can never be perturbed, so E3 is permanently inconclusive."""
    for language in sorted(shims.TEMPLATES):
        table = module._perturbations_for(language)
        assert len(table) >= 4, (
            "%s has only %d perturbations, which is too few to distinguish a subject"
            % (language, len(table)))


def test_an_unknown_language_falls_back_to_shared_comparisons():
    """Better a small set of valid mutants than a large set of broken ones.

    Comparison operators are spelled identically in every language this factory serves, so the
    fallback is safe. `channels_bite` already reports INCONCLUSIVE when nothing can be perturbed,
    which is an honest answer -- unlike a mutant that fails to build and is billed to the candidate.
    """
    table = module._perturbations_for("some-language-we-have-never-seen")
    assert table == module._COMPARISON
    for original, replacement in table:
        for spelling in ("id(", "sorted(", " and ", "[-1]"):
            assert spelling not in (original, replacement)


@pytest.mark.parametrize("alias,canonical", [
    ("typescript", "javascript"), ("ts", "javascript"), ("js", "javascript"),
    ("c++", "cpp"), ("golang", "go"), ("py", "python"),
])
def test_language_aliases_resolve_to_the_same_table(alias, canonical):
    """TypeScript reaching the fallback would silently weaken a language we actually serve."""
    assert module._perturbations_for(alias) == module._perturbations_for(canonical)


def test_python_mutants_are_still_valid_python():
    """The fix tightened Python's table too; Python is the language with attested artefacts."""
    source = ('def shorten(items, limit):\n'
              '    if len(items) >= limit:\n'
              '        return items[:limit]\n'
              '    total = 0\n'
              '    for i in range(len(items)):\n'
              '        total = total + len(items[i])\n'
              '    if total > 0 and limit > 0:\n'
              '        return sorted(items)\n'
              '    return items[-1]\n')
    mutants = 0
    for attempt in range(module.MUTATION_ATTEMPTS):
        mutated = module.mutate(source, "python", "shorten", attempt)
        compile(mutated, "<mutant>", "exec")
        mutants += mutated != source
    assert mutants == module.MUTATION_ATTEMPTS, (
        "only %d of %d attempts perturbed the source" % (mutants, module.MUTATION_ATTEMPTS))


def test_c_mutants_compile():
    """The regression, measured: three of C's four attempts used to be syntax errors.

    Uses the pointer declaration and `n++` that the bare `*` and `+` entries used to break.
    """
    if not shutil.which("cc"):
        pytest.skip("cc is not installed; cannot check generated C")
    source = ('#include <stddef.h>\n'
              'size_t shorten(const char *items, size_t limit) {\n'
              '    size_t n = 0;\n'
              '    while (items[n]) n++;\n'
              '    if (n >= limit) return limit;\n'
              '    size_t total = n + limit;\n'
              '    if (total > 0 && limit > 0) return total;\n'
              '    return n;\n'
              '}\n')
    built = 0
    for attempt in range(module.MUTATION_ATTEMPTS):
        mutated = module.mutate(source, "c", "shorten", attempt)
        if mutated == source:
            continue
        handle = tempfile.NamedTemporaryFile("w", suffix=".c", delete=False)
        try:
            handle.write(mutated)
            handle.close()
            done = subprocess.run(["cc", "-fsyntax-only", handle.name],
                                  capture_output=True, text=True)
            assert done.returncode == 0, (
                "C mutant %d does not compile: %s\n%s"
                % (attempt, done.stderr.strip().split("\n")[0], mutated))
            built += 1
        finally:
            os.unlink(handle.name)
    assert built >= 3, "only %d C mutants were produced at all" % built


def test_go_mutants_use_only_go_builtins():
    """Go has no `id`; `cap` is the builtin that survives the swap on a slice."""
    source = ('package main\n\n'
              'func Shorten(items []string, limit int) []string {\n'
              '\tif len(items) >= limit {\n'
              '\t\treturn items[:limit]\n'
              '\t}\n'
              '\ttotal := 0\n'
              '\tfor i := 0; i < len(items); i++ {\n'
              '\t\ttotal = total + len(items[i])\n'
              '\t}\n'
              '\tif total > 0 && limit > 0 {\n'
              '\t\treturn items\n'
              '\t}\n'
              '\treturn nil\n'
              '}\n')
    for attempt in range(module.MUTATION_ATTEMPTS):
        mutated = module.mutate(source, "go", "Shorten", attempt)
        if mutated == source:
            continue
        for spelling in ("id(", "sorted(", " and ", "[-1]", ".sort()"):
            assert spelling not in mutated or spelling in source, (
                "Go mutant %d introduced %r, which is not Go" % (attempt, spelling))
        # `i++` must survive: the bare `+` entry used to turn it into `i+-`.
        assert "i++" in mutated, "Go mutant %d damaged the `i++` increment" % attempt


def test_the_package_dispatcher_is_mutated_only_inside_entry():
    """The regression that took a whole TS batch down.

    A package subject is a generated dispatcher: `const DISPATCH = {...}` at top level, then
    `exports.entry = async function(op, ...args) { ... }`. `_window_of` could not find `entry`
    -- `exports.entry = async function(` matched no marker -- so it fell back to the WHOLE FILE,
    and the mutation table wrote into the top-level `const DISPATCH`, producing
    `const DISPATCH = { throw new Error('frf mutant'); ...` -- a syntax error. The subject then
    died when the shim tried to load it, surfaced as the one-line `Node.js v22.23.2`, and every
    candidate in the batch was refused at evidence as material.

    The window must find `exports.entry` (and `module.exports.entry`), so the top level is never
    a mutation site.
    """
    from frf.scales import module
    dispatcher = (
        'const DISPATCH = {"a": ["./mod", "a"], "b": ["./mod", "b"]};\n'
        'exports.entry = async function(op, ...args) {\n'
        '  if (!DISPATCH[op]) throw new Error("unknown operation: " + op);\n'
        '  const [mod, symbol] = DISPATCH[op];\n'
        '  let loaded;\n'
        '  try { loaded = await import(mod); } catch (e) { loaded = require(mod); }\n'
        '  const fn = loaded[symbol] || loaded.default;\n'
        '  if (typeof fn !== "function") throw new Error("not callable: " + symbol);\n'
        '  return fn(...args);\n'
        '};\n'
    )
    start, end = module._window_of(dispatcher, "entry")
    assert start != 0 or "DISPATCH" not in dispatcher[:start], (
        "window %r begins at the file start; the top-level DISPATCH is inside it" % (start,))
    # The DISPATCH line must never change under any mutation attempt.
    for attempt in range(module.MUTATION_ATTEMPTS):
        out = module.mutate(dispatcher, "typescript", "entry", attempt)
        assert out.splitlines()[0] == dispatcher.splitlines()[0], (
            "attempt %d mutated the top-level DISPATCH line" % attempt)
