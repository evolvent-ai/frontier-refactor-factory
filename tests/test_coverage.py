"""Coverage: the one measurement that cannot be language-agnostic, and must degrade rather than block."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf.core.scale import Spec                                        # noqa: E402
from frf.observe import coverage                                       # noqa: E402

_SUBJECT = '''
def never_called(x):
    return x * 999

def entry(args):
    n = args[0]
    if n < 0:
        raise ValueError("negative")
    if n > 100:
        return "big"
    return n * 2
'''


def _spec(path: str) -> Spec:
    return Spec(name="t", scale="module", language="python", description="d",
                invoke=["python3"], environment={"subject_path": path})


def _write_subject(directory: str, body: str = _SUBJECT) -> str:
    path = os.path.join(directory, "subject.py")
    with open(path, "w") as handle:
        handle.write(body)
    return path


def test_it_measures_real_lines_and_names_what_it_missed():
    """The fraction is the summary; the dark regions are what a repair loop can aim at."""
    with tempfile.TemporaryDirectory() as work:
        reach = coverage.backend_for("python").measure(
            _spec(_write_subject(work)), [[1], [2], [-5]])

        assert reach.measured and reach.backend == "python-trace"
        assert 0.0 < reach.fraction < 1.0
        assert reach.dark, "a fraction without regions gives a repair nothing to aim at"


def test_reaching_more_of_the_subject_raises_the_number():
    """The measurement has to respond to the corpus, or it is decoration."""
    with tempfile.TemporaryDirectory() as work:
        subject = _write_subject(work)
        narrow = coverage.backend_for("python").measure(_spec(subject), [[1]])
        wide = coverage.backend_for("python").measure(_spec(subject), [[1], [-5], [500]])

        assert wide.fraction > narrow.fraction, (wide.fraction, narrow.fraction)


def test_a_language_without_a_backend_is_an_absence_not_a_failure():
    """A task in an uninstrumented language still ships, with one number fewer and a note saying so.

    Refusing would trade the factory's language coverage for a tidier report, which is the larger
    cost -- and inventing the number would be worse than either.
    """
    backend = coverage.backend_for("haskell")
    reach = backend.measure(spec=None, probes=None)

    assert not reach.measured, "unmeasured, which is different from measuring zero"
    assert reach.backend == "none"
    assert "haskell" not in coverage.available()


def test_a_subject_it_cannot_find_reports_unmeasured_rather_than_zero():
    """Reporting our own failure as zero coverage would refuse tasks for a bug in here."""
    reach = coverage.backend_for("python").measure(_spec("/nonexistent/subject.py"), [[1]])
    assert not reach.measured


def test_docstrings_and_blank_lines_are_not_counted_against_a_subject():
    """Otherwise a well-documented subject reports low coverage for being well documented."""
    with tempfile.TemporaryDirectory() as work:
        documented = _write_subject(work, '''
"""A module docstring.

Several lines of it, none of which execute.
"""


def entry(args):
    """Also documented."""
    # A comment, too.
    return args[0]
''')
        reach = coverage.backend_for("python").measure(_spec(documented), [[1]])
        assert reach.fraction == 1.0, (reach.reached, reach.total, reach.dark)


# ------------------------------------------------------------------------------------------------
# The other seven backends. Everything above this line is Python's, which is the one that needs no
# toolchain; below, each language is skipped when its compiler or runtime is not on this machine.
# ------------------------------------------------------------------------------------------------
# One subject per language, all computing the same thing, all with a branch the narrow corpus never
# reaches. Written out rather than generated: a generator would need to know each language's syntax,
# which is exactly the knowledge this design claims not to need.
SUBJECTS = {
    "python": ("subject.py", '''\
def entry(args):
    values = args[0]
    if len(values) > 0:
        return sum(values)
    else:
        return -1
'''),
    "javascript": ("subject.js", '''\
module.exports.entry = (a) => {
  const values = a[0];
  if (values.length > 0) {
    return values.reduce((x, y) => x + y, 0);
  } else {
    return -1;
  }
};
'''),
    "ruby": ("subject.rb", '''\
def entry(args)
  values = args[0]
  if values.length > 0
    values.sum
  else
    -1
  end
end
'''),
    "go": ("subject.go", '''\
package main

func Entry(args []interface{}) (interface{}, error) {
\tvalues, _ := args[0].([]interface{})
\tif len(values) > 0 {
\t\ttotal := 0.0
\t\tfor _, x := range values {
\t\t\ttotal += x.(float64)
\t\t}
\t\treturn total, nil
\t}
\treturn -1, nil
}
'''),
    "c": ("subject.c", '''\
#include <stdlib.h>
#include <stdio.h>

char *entry_error = NULL;

const char *entry(const char *args_json)
{
    const char *p = args_json;
    double total = 0;
    int seen = 0;
    while (*p) {
        if (*p >= '0' && *p <= '9') { char *e; total += strtod(p, &e); p = e; seen = 1; }
        else p++;
    }
    if (seen) { char *o = malloc(64); snprintf(o, 64, "%.17g", total); return o; }
    else { entry_error = "empty"; return NULL; }
}
'''),
    "rust": ("subject.rs", '''\
pub fn entry(args: &crate::Json) -> Result<crate::Json, String> {
    let items = match args { crate::Json::Array(i) => i, _ => return Err("bad".to_string()) };
    let values = match items.first() {
        Some(crate::Json::Array(v)) => v,
        _ => return Err("bad".to_string()),
    };
    if !values.is_empty() {
        let mut total = 0.0;
        for x in values { if let crate::Json::Number(n) = x { total += n; } }
        Ok(crate::Json::Number(total))
    } else {
        Err("empty".to_string())
    }
}
'''),
    "java": ("Subject.java", '''\
import java.util.List;

public class Subject {
    public static Object entry(List<Object> args) {
        List<?> values = (List<?>) args.get(0);
        if (values.size() > 0) {
            double total = 0.0;
            for (Object x : values) { total += ((Number) x).doubleValue(); }
            return total;
        } else {
            return -1;
        }
    }
}
'''),
}

MEASURABLE = sorted(name for name in SUBJECTS if coverage.usable(name))


def _subject_for(work: str, language: str) -> Spec:
    name, body = SUBJECTS[language]
    path = os.path.join(work, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return Spec(name="t", scale="module", language=language, description="d",
                environment={"subject_path": path})


def test_every_language_the_design_names_has_a_backend():
    """The design commits to eight, and a table with three in it would still pass every other test.

    Stated as a test because "we support N languages" is the kind of claim that quietly becomes
    "we support the ones somebody got round to" -- and the whole argument for the wire is that the
    per-language cost is paid once, up front, rather than deferred forever.
    """
    for language in ("python", "javascript", "typescript", "go", "c", "cpp", "rust", "java",
                     "ruby"):
        assert language in coverage.available(), language


@pytest.mark.parametrize("language", MEASURABLE)
def test_this_language_measures_real_lines_and_names_what_it_missed(language):
    """Each backend, against a real subject with a branch the corpus never takes.

    The dark region is half the assertion. A fraction on its own tells a repair loop that the corpus
    is thin; naming the file it never touched is what lets the loop converge.
    """
    with tempfile.TemporaryDirectory() as work:
        spec = _subject_for(work, language)
        reach = coverage.backend_for(language).measure(spec, [[[1, 2, 3]], [[4, 5]], [[6]]])

        assert reach.measured, "%s reported nothing to measure with" % language
        assert 0.0 < reach.fraction < 1.0, (language, reach.reached, reach.total)
        assert reach.dark, "%s gave a fraction with no region to aim at" % language


@pytest.mark.parametrize("language", MEASURABLE)
def test_this_language_responds_to_how_much_the_corpus_reaches(language):
    """A backend that returned a constant would pass every other test in this file.

    This is the one that establishes the number MEANS something: widen the corpus so the other
    branch runs, and the fraction has to move. Measured for all eight, and all eight rise.
    """
    with tempfile.TemporaryDirectory() as work:
        spec = _subject_for(work, language)
        backend = coverage.backend_for(language)
        narrow = backend.measure(spec, [[[1, 2, 3]]])
        wide = backend.measure(spec, [[[1, 2, 3]], [[]]])

        assert narrow.measured and wide.measured, language
        assert wide.fraction > narrow.fraction, (
            "%s reported the same reach for a corpus that takes one branch and one that takes "
            "both (%.2f vs %.2f)" % (language, narrow.fraction, wide.fraction))


@pytest.mark.parametrize("language", sorted(SUBJECTS))
def test_this_language_never_raises_and_never_invents_a_zero(language):
    """A missing toolchain, a missing subject, a subject that does not compile.

    All three are UNMEASURED. The distinction from a measured zero is what decides whether a task
    ships: unmeasured is an absence adequacy states, and zero is a broken tracer that fails it. A
    backend that reported "could not run" as zero would refuse tasks for our own missing compiler.
    """
    backend = coverage.backend_for(language)

    missing = backend.measure(
        Spec(name="t", scale="module", language=language, description="d",
             environment={"subject_path": "/nonexistent/subject.src"}), [[1]])
    assert not missing.measured, language

    with tempfile.TemporaryDirectory() as work:
        name, _ = SUBJECTS[language]
        broken = os.path.join(work, name)
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("this is not valid source in any language !!! (((\n")
        reach = backend.measure(
            Spec(name="t", scale="module", language=language, description="d",
                 environment={"subject_path": broken}), [[1]])
        assert not reach.measured, (
            "%s reported a measured number for a subject that cannot even be built" % language)


def test_a_backend_and_its_toolchain_are_different_questions():
    """`available` is about this code; `usable` is about this machine.

    Collapsing them would make a laptop without a Go toolchain look like a factory that cannot
    serve Go, which is a statement about the design rather than about the laptop.
    """
    assert "go" in coverage.available()
    assert coverage.usable("go") == bool(shutil.which("go"))
    assert not coverage.usable("haskell")


def test_the_measurement_counts_the_subject_and_not_our_shim():
    """The shim is our code. Grading how much of IT ran would put a denominator in the report that
    no corpus of the subject could ever move, and it would be a large one."""
    with tempfile.TemporaryDirectory() as work:
        spec = _subject_for(work, "python")
        reach = coverage.backend_for("python").measure(spec, [[[1, 2, 3]]])
        # The subject is six lines. A number anywhere near the shim's size means the shim was
        # counted -- the Python shim alone is about fifty.
        assert reach.total <= 12, (reach.total, reach.dark)
