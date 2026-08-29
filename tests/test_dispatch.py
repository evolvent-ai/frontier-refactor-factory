"""The package scale's dispatcher generator: what it writes, and what it refuses to write.

Three things used to be decided by one boolean in the scale
(`native = language in ("javascript", "typescript")`), and all three were wrong
for the six languages that fell down the else-branch:

  * the dispatcher source itself -- a Rust task was handed PYTHON source,
  * the filename it was written to -- a second copy of what the shim already
    declares, which already disagreed with the TypeScript shim's `subject.ts`,
  * the mutant, built by APPENDING Python source to whatever the subject was.

The third is the one worth a test the most. A mutation gate exists to ask "would
the probe notice a subtly wrong implementation?" -- and appended Python is a
syntax error in every language but Python, so the mutant never loaded and the
probe "caught" it by not being able to parse it. That is a gate that passes
itself. The checks below pin the mutant to being a LOADABLE program that returns
a WRONG ANSWER, which is the only version of the mutant that says anything about
the probe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.call import dispatch                                       # noqa: E402
from frf.observe.call import shims                                          # noqa: E402

DISPATCH = {"stem": ("mypkg.stem", "stem"), "parse": ("mypkg.parse", "parse")}

# Both mutant attempts the gate uses. Kept explicit rather than derived so that
# adding a third wrong answer has to come here and be argued for.
ATTEMPTS = (0, 1)


def test_every_servable_language_has_a_shim_to_serve_it():
    """A dispatcher for a language with no shim cannot reach a subject at all.

    The scale asks the shim for the subject's filename, so a language supported
    here but absent from TEMPLATES would raise KeyError deep inside `build()`,
    in the sandbox, after the candidate was paid for.

    `languages()` is derived from the generators rather than listed beside them. A `DYNAMIC` tuple
    used to carry this meaning by hand, and its own comment warned that a second list is a second
    place to forget one -- which is precisely what happened when the ruby generator was added.
    """
    for language in dispatch.languages():
        assert language in shims.TEMPLATES, (
            "%s can be dispatched but has no shim to serve it" % language)
        assert dispatch.supported(language), (
            "%s is listed as dispatchable but has no generator" % language)


def test_a_mutant_is_a_wrong_answer_in_the_subjects_own_language():
    """A mutant that CRASHES is caught by the wire, not by the probe, and scores the gate too high.

    This was a two-column tuple indexed by `if language in ("javascript", "typescript")`, so ruby --
    being neither -- was handed Python's `None` and its mutants died with
    `NameError: uninitialized constant None`. Real ruby found it; the generated text looks fine.

    Keyed by language now, so a missing entry is a KeyError at generation time rather than a mutant
    that is mis-spelled in a way only that language's runtime can tell you about.
    """
    spellings = {"python": "None", "javascript": "null", "typescript": "null", "ruby": "nil"}
    for language in dispatch.languages():
        assert language in spellings, "%s has a generator but no wrong-answer spelling" % language
        missing = dispatch.source(language, DISPATCH, mutant=0)
        assert spellings[language] in missing, (
            "%s mutant 0 should return that language's own empty value" % language)
        falsy = dispatch.source(language, DISPATCH, mutant=1)
        assert "0" in falsy, language


def test_the_python_dispatcher_and_its_mutants_are_valid_python():
    """Source that does not compile is caught by the parser, not by the probe."""
    for mutant in (None,) + ATTEMPTS:
        source = dispatch.source("python", DISPATCH, mutant=mutant)
        compile(source, "<dispatcher>", "exec")


@pytest.mark.parametrize("language", ["javascript", "typescript"])
def test_the_javascript_dispatcher_and_its_mutants_are_valid_javascript(language):
    """The bug this pins: appended Python source made every JS mutant unparseable.

    Skipped rather than assumed when node is absent, because the structural
    checks in this file are meant to run on a bare host. TypeScript is verified
    with tsc --noEmit because `node --check` demands plain JS and the generated
    TS carries declarations.
    """
    if not shutil.which("node"):
        pytest.skip("node is not installed; cannot check generated JavaScript")
    if language == "typescript" and not shutil.which("tsc"):
        pytest.skip("tsc is not installed; cannot check generated TypeScript")
    for mutant in (None,) + ATTEMPTS:
        source = dispatch.source(language, DISPATCH, mutant=mutant)
        if language == "typescript":
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "subject.ts")
                with open(path, "w") as handle:
                    handle.write(source)
                done = subprocess.run(["tsc", "--target", "ES2022",
                                       "--module", "commonjs", "--skipLibCheck",
                                       "--noEmit", path],
                                      capture_output=True, text=True)
                assert done.returncode == 0, (
                    "%s mutant=%r does not type-check as TypeScript: %s"
                    % (language, mutant, done.stderr.strip()[:200]))
            continue
        handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        try:
            handle.write(source)
            handle.close()
            done = subprocess.run(["node", "--check", handle.name],
                                  capture_output=True, text=True)
            assert done.returncode == 0, (
                "%s mutant=%r does not parse as JavaScript: %s"
                % (language, mutant, done.stderr.strip()[:200]))
        finally:
            os.unlink(handle.name)


def test_a_mutant_answers_wrongly_rather_than_refusing():
    """The whole point of the gate: a mutant that raises is detected by the wire.

    If the mutant crashes, the probe is credited with catching something it never
    had to reason about, and the gate scores far too generously. So the mutant
    must answer every operation the real dispatcher answers -- with a wrong
    value.
    """
    real = dispatch.source("python", DISPATCH)
    assert "importlib" in real, "the real dispatcher should resolve modules"

    for attempt in ATTEMPTS:
        namespace: dict = {}
        exec(dispatch.source("python", DISPATCH, mutant=attempt), namespace)
        for operation in DISPATCH:
            answer = namespace["entry"](operation, "running")
            assert answer in (None, 0), (
                "mutant %d answered %r, which is not a recognisably wrong value"
                % (attempt, answer))


def test_the_mutants_differ_from_each_other():
    """Two attempts that produce the same subject test the probe once, not twice."""
    sources = {dispatch.source("python", DISPATCH, mutant=attempt)
               for attempt in ATTEMPTS}
    assert len(sources) == len(ATTEMPTS), "the mutation attempts are not distinct"


@pytest.mark.parametrize("language", ["go", "rust", "c", "cpp", "java"])
def test_a_language_without_a_dispatcher_refuses_loudly(language):
    """Silence here is what produced Python source in a file named `subject.rs`.

    These five have no runtime module-by-name lookup, so a package dispatcher for
    them is generated static imports plus a switch, with a concrete type per
    argument. That is real work; until it exists the honest answer is a refusal
    that names itself, which is the only kind of gap that can be argued with.
    """
    assert not dispatch.supported(language)
    with pytest.raises(dispatch.Unsupported) as raised:
        dispatch.source(language, DISPATCH)
    assert language in str(raised.value)


def test_the_dispatcher_covers_every_operation_it_was_given():
    """A dispatcher missing an entry point fails the contract, not the candidate.

    THE MODULE IS CHECKED IN EITHER SPELLING. The contract names a module the way Python spells one --
    `mypkg.stem` -- because one shape has to serve every language. Ruby has no such namespace: its
    dispatcher rewrites the dots to a path for `require_relative`, so demanding the dotted form here
    would fail a dispatcher that is correct, and demanding only the path form would fail the other
    three.
    """
    for language in dispatch.languages():
        source = dispatch.source(language, DISPATCH)
        for operation, (module, symbol) in DISPATCH.items():
            assert operation in source, (
                "%s dispatcher omits operation %s" % (language, operation))
            assert module in source or module.replace(".", "/") in source, (
                "%s dispatcher omits module %s in any spelling" % (language, module))
