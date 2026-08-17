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


def test_correctness_unlocks_speed_rather_than_being_averaged_with_it():
    """Short of complete, no amount of speed helps; complete, 0.5 is banked and speed adds on top."""
    # Fast and wrong. Capped at 0.5 * correctness -- it cannot reach the speed term at all.
    fast_and_wrong = scoring.compute(9, 10, speedup=100.0)
    assert fast_and_wrong.reward == 0.5 * 0.9, fast_and_wrong
    assert not fast_and_wrong.correct

    # Correct and unchanged: 0.5 for the behaviour, plus 0.5 * 1.0 for a speedup of exactly 1.
    unchanged = scoring.compute(10, 10, speedup=1.0)
    assert unchanged.reward == 1.0, unchanged

    # Monotone in speedup, and deliberately UNCAPPED -- comparability with the sibling benchmark
    # outweighs the tidiness of a bounded curve.
    rewards = [scoring.compute(10, 10, speedup=r).reward for r in (1.0, 1.5, 2.0, 8.0, 299.0)]
    assert rewards == sorted(rewards), rewards
    assert rewards[-1] > 100, "the scale is not capped"


def test_partial_correctness_is_a_gradient_not_a_cliff():
    """Missing three cases of 1567 and missing half of them are different results.

    A binary pass/fail collapses them, and they call for opposite next steps -- repair the existing
    implementation, or start over. The score has to be able to tell them apart.
    """
    nearly = scoring.compute(1564, 1567)
    barely = scoring.compute(800, 1567)
    assert nearly.reward > barely.reward > 0.0, (nearly.reward, barely.reward)
    assert nearly.reward < 0.5, "incomplete work never reaches the correctness credit"


def test_a_flagged_submission_scores_zero_whatever_else_it_did():
    """Compliance is independent of the other two axes and short-circuits both.

    A submission that delegated its work has not earned partial credit for the parts it did
    honestly, so this is zero rather than `0.5 * correctness`.
    """
    cheated = scoring.compute(10, 10, speedup=50.0, compliant=False)
    assert cheated.reward == 0.0, cheated
    assert not cheated.correct


def test_several_workloads_aggregate_geometrically():
    """Twice as fast here and half as fast there is break-even, and only the geometric mean says so."""
    assert scoring.aggregate_speedup({"render": 4.0, "parse": 0.25}) == 1.0
    # The arithmetic mean would report 2.125x for the same pair and call a wash an improvement.
    assert scoring.aggregate_speedup({"a": 2.0, "b": 2.0}) == 2.0
    assert scoring.aggregate_speedup({}) == 1.0, "no timed workload is neutral, not an error"


def test_timing_reports_a_gain_it_cannot_distinguish_from_noise_as_exactly_one():
    """The measurement that motivates this module: the same program, timed against itself.

    The true speedup is exactly 1.000, and a naive ratio on a shared machine wanders far from it.
    What comes back must be 1.0 in the FIELD A SCORE READS -- not the raw wandering ratio with a
    flag beside it, because then every caller has to remember to check the flag and the one that
    forgets pays for noise.
    """
    import random
    rng = random.Random(7)

    # A subject whose cost is genuinely constant, observed through a noisy clock. The noise is
    # one-sided, as real interference is: something else on the machine can only ever slow you down.
    def noisy(_probe):
        return 0.010 * (1.0 + abs(rng.gauss(0, 0.35)))

    result = timing.measure(noisy, noisy, lambda shape, i: i, ["only"], samples=12)
    assert result.speedup == 1.0, result
    assert result.within_noise, result
    assert result.measured is not None, "what was rejected is still reported"
    assert scoring.compute(10, 10, speedup=result.speedup).reward == 1.0


def test_timing_reports_a_real_gain_and_takes_the_worst_shape():
    """A candidate genuinely twice as fast is measured as such -- and cannot hide a bad shape."""
    def reference(_probe):
        return 0.020

    def twice_as_fast(_probe):
        return 0.010

    good = timing.measure(reference, twice_as_fast, lambda s, i: i, ["small", "large"], samples=12)
    assert not good.within_noise and good.usable, good
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


def test_an_exact_cost_measure_is_not_treated_as_noise():
    """Cost need not be wall-clock, and an exact measure must survive the noise machinery intact.

    A routine in a closed simulator reports cycle counts: identical across runs, so the reference
    against itself has zero spread and the floor lands at exactly 1.0. A genuine 2x must then be
    reported as 2x rather than discarded -- if the statistics designed for a noisy clock swallowed
    an exact measurement, the pluggable cost would be pluggable in name only.
    """
    def cycles_reference(_probe):
        return 1_000_000.0                       # deterministic: a simulator, not a stopwatch

    def cycles_candidate(_probe):
        return 500_000.0

    result = timing.measure(cycles_reference, cycles_candidate,
                            lambda shape, i: i, ["only"], samples=12)
    assert result.noise_floor == 1.0, "an exact cost has no noise to calibrate"
    assert not result.within_noise, result
    assert result.speedup == 2.0, result.speedup
