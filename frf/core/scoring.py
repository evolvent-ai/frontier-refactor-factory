"""Turning a graded run into a score.

This module never learns what an observation looks like. It is handed two counts and two clocks, and
that is the whole of its input -- which is why it is the piece that is provably shared by all four
scales rather than merely believed to be.

Two decisions are encoded here.

CORRECTNESS IS A GATE, SPEED IS A SLOPE. A submission that does not reproduce the reference's
behaviour scores zero however fast it is; there is no partial credit for being wrong quickly. Once
correct, the score rises continuously with measured speedup instead of stepping over a threshold.

THE SLOPE MUST NOT START AT ZERO SPEEDUP. On a same-language task the reference IS the solver's
starting point, so submitting it unchanged is trivially correct. Pure pass/fail correctness would
make that full marks. Scoring speed from 1.0x upwards means the unchanged submission earns the
floor -- non-zero, because it is genuinely correct -- and every real gain is paid for in proportion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# The band a speedup is mapped into. TARGET earns full marks; it is a scoring parameter and NOT a
# gate, so exceeding it simply saturates. No threshold is validated in advance: whether a given
# subject holds 1.2x or 5x is not knowable before someone tries, and pretending to know it is how
# tasks end up either trivially passed or impossible.
CORRECT_FLOOR = 0.15
SPEED_TARGET = 2.0


def speed_score(speedup: float, *, target: float = SPEED_TARGET,
                floor: float = CORRECT_FLOOR) -> float:
    """Map a speedup ratio into [floor, 1.0], logarithmically.

    Logarithmic because a speedup is a ratio: 1x->2x is the same achievement as 2x->4x, and a linear
    map would pay far more for the second. Below 1.0x the score stays at the floor rather than going
    negative -- the submission is still correct, and correctness has already been established.
    """
    if not math.isfinite(speedup) or speedup <= 1.0:
        return floor
    fraction = math.log(speedup) / math.log(max(target, 1.0000001))
    return floor + (1.0 - floor) * min(1.0, fraction)


@dataclass(frozen=True)
class Reward:
    """One submission's score, carrying the numbers it was computed from.

    Everything needed to recompute the result travels with it. A reward nobody can check is a claim
    rather than a measurement, and these numbers are quoted in reports long after the run.
    """

    reward: float
    passed: int
    total: int
    correct: bool
    speedup: float
    reference_seconds: float
    candidate_seconds: float
    note: str = ""

    def to_json(self) -> dict:
        return {"reward": round(self.reward, 6),
                "correct": self.correct,
                "correctness_passed": self.passed,
                "correctness_total": self.total,
                "speedup": round(self.speedup, 4),
                "reference_seconds": round(self.reference_seconds, 4),
                "candidate_seconds": round(self.candidate_seconds, 4),
                "note": self.note}


def compute(passed: int, total: int, reference_seconds: float, candidate_seconds: float, *,
            target: float = SPEED_TARGET, scored_on_speed: bool = True) -> Reward:
    """Graded observations and two clocks -> the score."""
    correct = total > 0 and passed == total
    if not correct:
        return Reward(0.0, passed, total, False, 0.0, reference_seconds, candidate_seconds,
                      "behaviour does not match the reference; speed was not scored")
    if not scored_on_speed:
        # Behaviour was the whole task, so full marks -- emphatically not `floor`, which would pay
        # a fully correct submission 0.15 for clearing the only bar the task actually sets.
        #
        # Reaching here on a SAME-LANGUAGE task would be a defect upstream rather than here: the
        # reference is in the solver's hands, so `cp` earns this. The pipeline refuses to emit such
        # a task; this function is right to pay full marks once one exists.
        return Reward(1.0, passed, total, True, 0.0, reference_seconds, candidate_seconds,
                      "correct; this task is scored on behaviour alone")
    speedup = (reference_seconds / candidate_seconds) if candidate_seconds > 0 else 0.0
    return Reward(speed_score(speedup, target=target), passed, total, True, speedup,
                  reference_seconds, candidate_seconds,
                  "correct; scored on measured speedup (target %.1fx)" % target)
