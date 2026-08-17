"""The process seam: four channels, masked by position, and the deletion it must not permit."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.process.observation import (                          # noqa: E402
    CHANNELS, Expectation, Observation, Stream, freeze, grade)


def _observed(exit_code: int, out: str = "", err: str = "", tree: str = "") -> Observation:
    return Observation(exit_code, Stream.of(out), Stream.of(err), Stream.of(tree))


def test_a_reproducible_step_grades_every_channel():
    runs = [_observed(0, "hello\nworld", "", "a.txt") for _ in range(5)]
    expectation = freeze(0, runs)
    assert expectation.graded_points() == len(CHANNELS), expectation.to_json()

    passed, total, reasons = grade(expectation, runs[0])
    assert (passed, total) == (4, 4), reasons


def test_one_moving_line_is_masked_and_the_rest_still_grades():
    """A clock in the output must not cost the whole stream.

    Dropping the stream was the alternative, and it is worse: it silently stops grading everything
    the program prints, which is usually all the evidence there is.
    """
    runs = [_observed(0, "start\ntook %dms\ndone" % ms) for ms in (11, 12, 13, 14, 15)]
    expectation = freeze(0, runs)

    stdout = expectation.channel("stdout")
    assert stdout.graded and stdout.masked == frozenset({1}), stdout.to_json()

    # A candidate that reproduces the stable lines passes, whatever it puts on the masked line.
    passed, total, reasons = grade(expectation, _observed(0, "start\ntook 9999ms\ndone"))
    assert (passed, total) == (4, 4), reasons

    # ...and one that changes a GRADED line still fails.
    passed, _, reasons = grade(expectation, _observed(0, "start\ntook 12ms\nDONE"))
    assert passed == 3 and any("stdout" in r for r in reasons), reasons


def test_deleting_a_line_cannot_hide_inside_the_mask():
    """The defect that makes masking sound only when the line count is checked first.

    With line 1 masked, a submission that DELETES line 1 shifts "done" up into the masked slot. If
    the digest were compared without checking the count, the mask would hide the deletion and the
    submission would score a point for removing output.
    """
    runs = [_observed(0, "start\ntook %dms\ndone" % ms) for ms in (11, 12, 13, 14, 15)]
    expectation = freeze(0, runs)

    passed, total, reasons = grade(expectation, _observed(0, "start\ndone"))
    assert passed == 3 and total == 4, (passed, total)
    assert any("expected 3 line(s), got 2" in r for r in reasons), reasons


def test_a_channel_that_never_settles_is_not_graded_rather_than_wrong():
    """An unstable channel contributes to neither count: it is not a point anyone can fail."""
    runs = [_observed(0, "nonce %d" % i) for i in range(5)]
    expectation = freeze(0, runs)

    stdout = expectation.channel("stdout")
    assert not stdout.graded and "varies" in stdout.reason, stdout.to_json()

    passed, total, _ = grade(expectation, _observed(0, "anything at all"))
    assert (passed, total) == (3, 3), "the other three channels still grade"


def test_a_moving_line_count_disables_the_channel_entirely():
    """No stable coordinate, no mask. Line 7 of one run is not line 7 of another."""
    runs = [_observed(0, "\n".join(str(x) for x in range(n))) for n in (3, 4, 5, 3, 4)]
    stdout = freeze(0, runs).channel("stdout")
    assert not stdout.graded and "line counts" in stdout.reason, stdout.to_json()


def test_channels_are_independent():
    """A clock on stderr must not cost the exit code, the stdout or the files."""
    runs = [_observed(3, "stable", "at %d" % i, "out.bin") for i in range(5)]
    expectation = freeze(0, runs)
    assert not expectation.channel("stderr").graded
    assert expectation.graded_points() == 3, expectation.to_json()

    passed, total, _ = grade(expectation, _observed(3, "stable", "anything", "out.bin"))
    assert (passed, total) == (3, 3)


def test_an_expectation_survives_a_round_trip_through_json():
    """It ships in the package; if it cannot be reloaded exactly, nothing downstream is trustworthy."""
    runs = [_observed(0, "start\ntook %dms" % ms, "", "a.txt") for ms in (1, 2, 3, 4, 5)]
    original = freeze(2, runs)
    restored = Expectation.from_json(original.to_json())

    assert restored.to_json() == original.to_json()
    assert restored.channel("stdout").masked == frozenset({1})
    assert grade(restored, runs[0]) == grade(original, runs[0])
