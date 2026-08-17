"""Adequacy: the axis evidence cannot see.

The framing test is the first one. A corpus of `git --version` passes every evidence check there is
and tests nothing, so adequacy has to be able to fail something evidence would pass.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf.core import adequacy                                          # noqa: E402


def test_a_corpus_that_grades_constants_is_refused_however_green_its_evidence():
    """`git --version` reproduces, rejects a blank submission, and bites on every channel. What it
    cannot do is distinguish an implementation, and only the floor says so."""
    reach = adequacy.Reach(reached=12, total=10_000, dark=("commit.c", "merge.c"), backend="gcov")
    floor = adequacy.measure_floor(lambda name: (95, 100), ("exit-0", "exit-1", "print-nothing"))

    report = adequacy.assess(reach, floor)
    assert not report.ok
    assert not report.reach_ok, "12 of 10000 lines is not reaching the subject"
    assert "commit.c" in report.note, "a refusal has to say where to aim the repair"


def test_the_floor_takes_the_worst_trivial_submission_not_the_first():
    """Trying one is trying your luck: on a real task `exit 0` scored 50% and `exit 1` scored 75%."""
    scores = {"exit-0": (50, 100), "exit-1": (75, 100), "print-nothing": (10, 100)}
    floor = adequacy.measure_floor(lambda name: scores[name], tuple(scores))

    assert floor.fraction == 0.75 and floor.worst == "exit-1"
    assert set(floor.attempts) == set(scores), "every attempt is reported, not just the worst"


def test_a_high_floor_fails_even_with_excellent_reach():
    """Executing the whole program means nothing if almost none of what it does is graded."""
    reach = adequacy.Reach(reached=980, total=1000, backend="gcov")
    floor = adequacy.measure_floor(lambda _n: (90, 100), ("exit-0",))

    report = adequacy.assess(reach, floor)
    assert report.reach_ok and not report.floor_ok and not report.ok
    assert "does nothing already scores" in report.note


def test_no_backend_is_an_absence_and_ships_saying_so():
    """A language nobody instrumented still produces tasks; it produces them with one fewer number.

    Refusing would restrict which languages this factory serves, which is a bigger cost than a
    missing statistic -- and inventing the statistic would be worse than either.
    """
    reach = adequacy.NullCoverage().measure(spec=None, probes=None)
    assert not reach.measured and reach.backend == "none"

    report = adequacy.assess(reach, adequacy.measure_floor(lambda _n: (10, 100), ("exit-0",)))
    assert report.ok, "an unmeasured reach cannot fail"
    assert "not measured" in report.note and "no backend" in report.note


def test_a_measured_zero_is_a_broken_tracer_and_is_not_the_same_as_no_backend():
    """The distinction that lets 0/0 sail through a gate if it is not made.

    A backend that ran and found nothing executed did not discover an empty corpus; it failed to
    attach. `measured` is False only when there was nothing to measure with.
    """
    no_backend = adequacy.Reach(backend="none")
    broken = adequacy.Reach(reached=0, total=5000, backend="gcov")

    assert not no_backend.measured
    assert broken.measured, "a backend that reports a denominator has run"
    assert not adequacy.assess(broken, adequacy.Floor()).reach_ok
