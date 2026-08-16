"""The battery, checked against the verifiers it exists to catch.

The important tests here are not "a good task passes". They are: does a verifier that scores
everything zero get through, and does one that scores everything full marks get through? Those two
constants are what a one-sided battery accepts, and accepting either makes the whole thing
decoration.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import evidence                                          # noqa: E402
from frf.core.evidence import Outcome                                  # noqa: E402


def test_a_verifier_that_rejects_everything_is_caught_by_the_ceiling():
    """The failure mode a floor-only battery accepts forever.

    "No bad submission scores marks" is satisfied completely by a verifier that gives nothing to
    anybody -- including the reference. Only the ceiling notices.
    """
    always_zero = evidence.ceiling(lambda: (0, 40))
    assert always_zero.outcome is Outcome.FAILS
    assert "no submission could do better" in always_zero.detail

    # And that same verifier sails through the floor, which is the point.
    floor = evidence.floor(lambda name: (0, 40), ["do-nothing", "always-fail"])
    assert floor.outcome is Outcome.HOLDS


def test_a_verifier_that_accepts_everything_is_caught_by_the_floor():
    """The mirror image: full marks for the reference proves nothing on its own."""
    assert evidence.ceiling(lambda: (40, 40)).outcome is Outcome.HOLDS
    generous = evidence.floor(lambda name: (40, 40), ["do-nothing"])
    assert generous.outcome is Outcome.FAILS
    assert "full marks" in generous.detail


def test_the_floor_takes_the_worst_trivial_submission_not_the_first():
    """Which trivial submission scores highest depends on the subject, so several are tried.

    Trying one measures luck: a subject whose success is silence is matched by a submission that
    prints nothing, and a subject that always refuses is matched by one that always refuses.
    """
    scores = {"do-nothing": (5, 40), "always-fail": (30, 40)}
    verdict = evidence.floor(lambda name: scores[name], list(scores))
    assert verdict.outcome is Outcome.HOLDS
    assert "always-fail" in verdict.detail, "the worst case is what gets reported"
    assert "75%" in verdict.detail


def test_a_channel_that_cannot_be_made_to_diverge_is_inconclusive_not_a_pass():
    """The distinction that stops this check from being a rubber stamp.

    A mutant scoring full marks is ambiguous -- blind verifier, or a mutation that touched nothing
    graded. Inferring from the score cannot separate them, so a perturbation that did not provably
    change an observation proves nothing and must not be counted as evidence.
    """
    # Nothing diverged: the checks says so rather than reporting success.
    inert = evidence.channels_bite(lambda ch: (False, True), ["value", "error"])
    assert inert.outcome is Outcome.INCONCLUSIVE
    assert not inert.ok, "inconclusive must not let a task ship"

    # Diverged and was caught: real evidence.
    good = evidence.channels_bite(lambda ch: (True, True), ["value", "error"])
    assert good.outcome is Outcome.HOLDS

    # Diverged and was NOT caught: the verifier is blind on that channel.
    blind = evidence.channels_bite(lambda ch: (True, ch != "error"), ["value", "error"])
    assert blind.outcome is Outcome.FAILS
    assert "error" in blind.detail


def test_a_partially_inert_battery_still_reports_which_channels_were_not_shown():
    """Honest about what was demonstrated: some evidence is not the same as all of it."""
    verdict = evidence.channels_bite(lambda ch: (ch == "value", True), ["value", "error"])
    assert verdict.outcome is Outcome.HOLDS
    assert "error" in verdict.detail and "could not be made to diverge" in verdict.detail


def test_both_directories_are_checked_for_a_runnable_reference():
    """The verifier's own directory is the half usually missed.

    It holds a runnable reference and every expectation; whether a submission can read it is decided
    by one setting, and if that setting is ever wrong, reading the answer and replaying it scores
    full marks.
    """
    def find(path):
        return ["reference-binary"] if "tests" in path else []

    verdict = evidence.no_runnable_reference(
        find, {"solver tree": "/task/environment/reference", "verifier": "/task/tests"})
    assert verdict.outcome is Outcome.FAILS
    assert "verifier" in verdict.detail

    clean = evidence.no_runnable_reference(
        lambda p: [], {"solver tree": "/a", "verifier": "/b"})
    assert clean.outcome is Outcome.HOLDS


def test_the_emitted_package_is_driven_with_its_own_reference():
    """Everything else measures the build tree; this opens what was actually written."""
    assert evidence.package_reproduces_itself(lambda: (231, 231)).outcome is Outcome.HOLDS

    incomplete = evidence.package_reproduces_itself(lambda: (75, 240))
    assert incomplete.outcome is Outcome.FAILS
    assert "shipped beside it" in incomplete.detail

    empty = evidence.package_reproduces_itself(lambda: (0, 0))
    assert empty.outcome is Outcome.INCONCLUSIVE, "grading nothing is not a pass"


def test_expectations_must_not_encode_this_run_s_hash_order():
    stable = evidence.seed_independent(lambda seed: "sha256:same", [1, 2, 3])
    assert stable.outcome is Outcome.HOLDS

    wobbly = evidence.seed_independent(lambda seed: "sha256:%d" % seed, [1, 2, 3])
    assert wobbly.outcome is Outcome.FAILS
    assert "this run's ordering" in wobbly.detail

    assert evidence.seed_independent(lambda s: "x", [1]).outcome is Outcome.INCONCLUSIVE


def test_a_battery_is_not_ok_until_something_has_been_checked():
    """An empty battery must not read as success -- that is how a task ships unexamined."""
    empty = evidence.Battery()
    assert not empty.ok

    battery = evidence.Battery()
    battery.record(evidence.ceiling(lambda: (10, 10)))
    battery.record(evidence.floor(lambda n: (2, 10), ["do-nothing"]))
    assert battery.ok and not battery.failures()

    battery.record(evidence.package_reproduces_itself(lambda: (9, 10)))
    assert not battery.ok
    assert [v.check for v in battery.failures()] == ["package-reproduces-itself"]


def test_inconclusive_is_not_ok_but_not_applicable_is():
    """A check that cannot fail on this seam is fine; a check that could not tell is not.

    The difference is the whole distance between a battery and a ritual: "structurally impossible
    here" is a reason, "we did not manage to show it" is a gap.
    """
    assert evidence.Verdict("x", Outcome.NOT_APPLICABLE).ok
    assert not evidence.Verdict("x", Outcome.INCONCLUSIVE).ok
