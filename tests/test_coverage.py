"""Coverage: the one measurement that cannot be language-agnostic, and must degrade rather than block."""
from __future__ import annotations

import os
import sys
import tempfile

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
