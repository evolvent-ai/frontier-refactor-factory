"""Turning a graded run into a score.

This module never learns what an observation looks like. It is handed two counts and a vouched-for
ratio, and that is the whole of its input -- which is why it is the piece that is provably shared by
all four scales rather than merely believed to be.

    score = 0.5 + 0.5 * speedup      when correctness is 1.0
    score = 0.5 * correctness        otherwise

CORRECTNESS IS AN UNLOCK, NOT A WEIGHT. Short of complete, a submission cannot earn any speed credit
at all -- it is capped at `0.5 * correctness` however fast it runs, because a fast wrong answer is
not a partial right one. Complete, it banks 0.5 for that alone and the speed term is added on top.

PARTIAL CORRECTNESS IS REPORTED, NOT ROUNDED AWAY. `0.5 * correctness` is a real gradient, and it
exists because "missed three cases of 1567" and "missed half of them" are different outcomes with
different next steps. A binary pass/fail hides that difference at exactly the moment it matters.

NO CAP, NO LOG, NO THRESHOLD. An earlier draft mapped the log of the speedup into a bounded band and
saturated at a target. That curve is better behaved -- it is bounded, and one lucky 299x cannot
dominate a suite -- and it is nevertheless the wrong choice here: this benchmark reports alongside
FrontierSWE-Performance, and a score that cannot be compared across benchmarks is worth less than a
score with an ugly tail. Comparability beats curve design.

Speed is scored only against a ratio the measurement layer already vouched for. A difference inside
the machine's own noise arrives here as exactly 1.0, so "no measurable change" contributes nothing
beyond the 0.5 correctness already earned. That substitution belongs to `timing`, which alone knows
what this machine's noise was: by the time a number reaches this module it is a finding, not a
reading.
"""
from __future__ import annotations

from dataclasses import dataclass

# What complete correctness is worth before any speed is measured. The split is even: half the score
# for reproducing the behaviour, half available for making it faster.
CORRECTNESS_CREDIT = 0.5


@dataclass(frozen=True)
class Reward:
    """One submission's score, carrying the numbers it was computed from.

    Everything needed to recompute the result travels with it. A reward nobody can check is a claim
    rather than a measurement, and these numbers get quoted in reports long after the run.
    """

    reward: float
    passed: int
    total: int
    correct: bool
    correctness: float
    speedup: float
    note: str = ""

    def to_json(self) -> dict:
        return {"reward": round(self.reward, 6),
                "correct": self.correct,
                "correctness": round(self.correctness, 6),
                "correctness_passed": self.passed,
                "correctness_total": self.total,
                "speedup": round(self.speedup, 4),
                "note": self.note}


def compute(passed: int, total: int, speedup: float = 1.0, *, compliant: bool = True) -> Reward:
    """Graded observations and a vouched-for speedup -> the score.

    `speedup` must already have been through the measurement layer, which substitutes 1.0 for any
    ratio inside this machine's noise. Passing a raw stopwatch ratio here would score the machine.

    `compliant` is the anti-circumvention verdict, independent of the other two axes: a submission
    that imported the reference or delegated its work has not earned partial credit for the parts it
    did honestly, so this short-circuits to zero before anything else is considered.
    """
    if not compliant:
        return Reward(0.0, passed, total, False, 0.0, 0.0,
                      "compliance check failed; neither behaviour nor speed was scored")

    correctness = (passed / total) if total > 0 else 0.0
    if total <= 0 or passed < total:
        return Reward(CORRECTNESS_CREDIT * correctness, passed, total, False, correctness, 0.0,
                      "behaviour is incomplete (%d/%d); speed is not scored until it is complete"
                      % (passed, total))

    return Reward(CORRECTNESS_CREDIT + CORRECTNESS_CREDIT * speedup,
                  passed, total, True, 1.0, speedup,
                  "correct; scored on measured speedup (%.4gx)" % speedup)
