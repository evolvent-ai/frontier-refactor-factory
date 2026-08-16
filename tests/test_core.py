"""Tests that run the real thing.

No mocks. A mocked test of a factory whose entire purpose is to measure what real programs really do
would assert that the mock behaves as written, which is never the thing in doubt.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import scoring, timing                                  # noqa: E402


def test_correctness_is_a_gate_and_speed_is_a_slope():
    """A wrong submission scores zero however fast; a correct one is paid in proportion."""
    fast_and_wrong = scoring.compute(9, 10, 1.0, 0.01)
    assert fast_and_wrong.reward == 0.0, fast_and_wrong
    assert not fast_and_wrong.correct

    # Correct but no faster: the floor, not zero. It IS a correct implementation.
    unchanged = scoring.compute(10, 10, 1.0, 1.0)
    assert unchanged.reward == scoring.CORRECT_FLOOR, unchanged

    # And every real gain is paid for, monotonically, saturating at the target.
    rewards = [scoring.compute(10, 10, r, 1.0).reward for r in (1.0, 1.25, 1.5, 2.0, 8.0)]
    assert rewards == sorted(rewards), rewards
    assert rewards[-2] == 1.0 and rewards[-1] == 1.0, "the target saturates rather than overflowing"


def test_a_task_with_no_clock_still_pays_full_marks_for_being_correct():
    """`scored_on_speed=False` means behaviour was the whole task, so correctness earns 1.0.

    Paying the floor here would be the opposite error: docking a submission for not improving a
    speed it was never asked to improve. Whether a task is ALLOWED to be scored this way is decided
    upstream -- a same-language task may not be, because its reference is the solver's own starting
    point -- and that decision does not belong in the arithmetic.
    """
    assert scoring.compute(10, 10, 0.0, 0.0, scored_on_speed=False).reward == 1.0
    assert scoring.compute(9, 10, 0.0, 0.0, scored_on_speed=False).reward == 0.0


def test_timing_refuses_a_gain_it_cannot_distinguish_from_noise():
    """The measurement that motivates this whole module: the same program, timed against itself.

    The true speedup is exactly 1.000. A naive ratio on a shared machine wanders far from it, so the
    gate must return `honest=False` rather than a number a score would happily consume.
    """
    import random
    rng = random.Random(7)

    # A subject whose cost is genuinely constant, observed through a noisy clock. The noise is
    # one-sided, as real interference is: something else on the machine can only ever slow you down.
    def noisy(_probe):
        return 0.010 * (1.0 + abs(rng.gauss(0, 0.35)))

    result = timing.measure(noisy, noisy, lambda shape, i: i, ["only"], samples=12)
    assert not result.honest, result
    assert "noise" in result.note, result.note


def test_timing_reports_a_real_gain_and_takes_the_worst_shape():
    """A candidate genuinely twice as fast is measured as such -- and cannot hide a bad shape."""
    def reference(_probe):
        return 0.020

    def twice_as_fast(_probe):
        return 0.010

    good = timing.measure(reference, twice_as_fast, lambda s, i: i, ["small", "large"], samples=12)
    assert good.honest, good
    assert 1.8 <= good.speedup <= 2.2, good.speedup

    # Fast on one shape, slower on the other. The worst shape is what counts, so this is refused:
    # tuning one convenient size is not making the subject faster.
    def fast_on_one_shape_only(probe):
        return 0.010 if probe % 2 == 0 else 0.030

    def draw(shape, i):
        return i if shape == "small" else i + 1

    mixed = timing.measure(reference, fast_on_one_shape_only, draw, ["small", "large"], samples=12)
    assert mixed.speedup < good.speedup, (mixed.speedup, good.speedup)


def test_credentials_come_from_one_place_and_never_appear_in_a_message():
    """A missing credential names the KEY, never a value -- and says where to put it."""
    from frf.core import credentials

    try:
        credentials.require("FRF_DEFINITELY_NOT_SET")
    except LookupError as exc:
        assert "FRF_DEFINITELY_NOT_SET" in str(exc)
        assert ".env" in str(exc), "the message says where the credential belongs"
    else:
        raise AssertionError("a missing credential must raise rather than return None")

    os.environ["FRF_TEST_ONLY_TOKEN"] = "sentinel-value"
    try:
        assert credentials.get("FRF_TEST_ONLY_TOKEN") == "sentinel-value"
    finally:
        del os.environ["FRF_TEST_ONLY_TOKEN"]


def test_no_tracked_file_carries_a_credential():
    """A key in a committed file has already leaked, whatever happens next.

    Checked by shape rather than by entropy: a prefix is what a scanner finds and what a reader
    recognises, and matching randomness would fire on every hash in the tree.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    listed = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    if listed.returncode != 0:
        return

    patterns = [(r"ghp_[A-Za-z0-9]{20,}", "GitHub token"),
                (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained token"),
                (r"\bsk-[A-Za-z0-9_-]{16,}", "API key"),
                (r"e2b_[A-Za-z0-9]{20,}", "sandbox key"),
                (r"AKIA[0-9A-Z]{16}", "AWS key")]
    found = []
    for rel in listed.stdout.split():
        path = os.path.join(root, rel)
        if not os.path.isfile(path) or os.path.getsize(path) > 2_000_000:
            continue
        text = open(path, encoding="utf-8", errors="ignore").read()
        for pattern, what in patterns:
            for match in re.finditer(pattern, text):
                # Redacted: a test that prints the secret to prove the secret is there has published
                # it again, into CI logs this time.
                found.append("%s: %s %s..." % (rel, what, match.group(0)[:7]))
    assert not found, found
