"""Measuring a speedup that is not the machine's mood.

Timing two programs and dividing is wrong on a shared machine. A ratio from one pair of stopwatch
readings carries the machine's state as much as the code's: a neighbouring job, a frequency change,
a cold cache on whichever ran first. So a single ratio is not evidence, and this module's job is to
turn many readings into one number that is.

Five things make it evidence, and each answers a specific way the naive measurement lies:

    INTERLEAVED       reference and candidate timed alternately in one run, so a machine that slows
                      down halfway through slows both rather than whichever went second.
    ORDER ALTERNATED  the one that runs first pays for a cold cache, so who goes first alternates.
    INPUT POLICY      supplied by the caller and reported by the delivered evaluator; no claim
                      that repeated held-out inputs prevent memoisation.
    NOISE FLOOR       calibrated HERE, on this machine, in this run, by timing the reference against
                      ITSELF -- a comparison whose true answer is exactly 1.0, so whatever spread
                      appears is this machine's noise and nothing else. Never a constant: a number
                      measured on the authoring host describes a machine nobody is graded on.
    LOWER BOUND       the bootstrapped confidence interval's lower bound is what gets reported, not
                      the median. Being 95% sure of at least 1.4x is a claim; a median of 1.4x with
                      this much spread is not.

And the worst shape counts, not the average: a candidate that tuned one convenient size has not made
the subject faster.

A GAIN INSIDE THE NOISE IS REPORTED AS EXACTLY 1.0. Not as itself with a warning, and not as a
failure -- as 1.0, the neutral value, because that is what the measurement actually established.
Scoring is downstream of this and must not have to re-litigate what counts as noise; by the time a
ratio leaves this module it is a finding.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Callable

# How many paired samples per shape. Below about ten the bootstrap has too little to resample and
# its interval is as unstable as the thing it is measuring.
SAMPLES_PER_SHAPE = 12
WARMUP_PAIRS = 2
PROTOCOL_VERSION = "paired-worst-v1"

# Resamples drawn to build the interval. Cheap -- it is arithmetic on numbers already collected --
# so this is set high enough that the bound does not wobble between runs on identical data.
BOOTSTRAP_DRAWS = 2000

# The interval's coverage. 0.95 means the reported bound is the 5th percentile of resampled medians.
CONFIDENCE = 0.95


@dataclass(frozen=True)
class SpeedResult:
    """What a timing pass concluded, with the evidence for it."""

    speedup: float                      # THE number a score uses; exactly 1.0 when within noise
    median: float                       # what a naive measurement would have reported
    noise_floor: float                  # reference vs itself, upper bound; a gain must clear this
    shapes: dict = field(default_factory=dict)
    within_noise: bool = False          # the gain did not clear the floor and was reported as 1.0
    measured: float | None = None       # the raw worst-shape bound, kept only when it was rejected
    usable: bool = True                 # False means nothing was measured at all -- not "no gain"
    note: str = ""
    evidence: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        out = {"protocol": PROTOCOL_VERSION, "evidence": self.evidence,
               "speedup": round(self.speedup, 4), "median": round(self.median, 4),
               "noise_floor": round(self.noise_floor, 4), "within_noise": self.within_noise,
               "usable": self.usable,
               "shapes": {k: round(v, 4) for k, v in self.shapes.items()}, "note": self.note}
        if self.measured is not None:
            out["measured"] = round(self.measured, 4)
        return out


def _bootstrap_lower_bound(ratios: list[float], *, draws: int = BOOTSTRAP_DRAWS,
                           confidence: float = CONFIDENCE, seed: int = 0) -> float:
    """The lower bound of the confidence interval on the median ratio.

    Seeded, because a gate whose verdict changes between identical runs is not a gate. The seed is
    fixed rather than drawn from the clock for the same reason the rest of this pipeline pins
    everything it can.
    """
    if not ratios:
        return 0.0
    if len(ratios) == 1:
        return ratios[0]
    import random
    rng = random.Random(seed)
    n = len(ratios)
    medians = []
    for _ in range(draws):
        medians.append(statistics.median(rng.choice(ratios) for _ in range(n)))
    medians.sort()
    index = int((1.0 - confidence) * len(medians))
    return medians[min(index, len(medians) - 1)]


def _paired_ratios(reference: Callable[[object], float], candidate: Callable[[object], float],
                   draw_probe: Callable[[int], object], samples: int,
                   observations: list | None = None) -> list[float]:
    """Time the two alternately on the same fresh probe. -> one ratio per sample."""
    ratios = []
    for i in range(samples):
        probe = draw_probe(i)
        # Order alternates: whichever runs first pays for whatever the other then finds warm.
        if i % 2 == 0:
            t_ref, t_cand = reference(probe), candidate(probe)
        else:
            t_cand, t_ref = candidate(probe), reference(probe)
        if not all(math.isfinite(t) and t > 0 for t in (t_ref, t_cand)):
            raise ValueError("measurement must be finite and strictly positive")
        ratio = t_ref / t_cand
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("invalid cost ratio")
        ratios.append(ratio)
        if observations is not None:
            observations.append({"reference": t_ref, "candidate": t_cand,
                                 "reference_first": i % 2 == 0})
    return ratios


def measure(reference: Callable[[object], float], candidate: Callable[[object], float],
            draw_probe: Callable[[str, int], object], shapes: list[str], *,
            samples: int = SAMPLES_PER_SHAPE, warmups: int = WARMUP_PAIRS) -> SpeedResult:
    """-> the speedup a score may use, which is the worst shape's lower bound.

    `reference` and `candidate` each measure the COST of one probe and return it. Keeping the
    measurement inside the callables is what lets a compiled subject be measured fairly: it times
    its own work rather than being charged for process startup and transport, which would otherwise
    dominate exactly the quick subjects this pipeline has the most of.

    COST IS NOT NECESSARILY TIME, and that is why these are callables returning a float rather than
    a stopwatch this module owns. Wall-clock is the default and the noisiest choice available; a
    routine running in a closed simulator can report cycles, which are exact and identical across
    runs, and a CPU kernel can report instructions retired or allocations made. Everything below --
    interleaving, alternating order, the noise floor, the bootstrap, taking the worst shape -- is
    arithmetic on ratios and does not care which cost was measured.

    A cost that is exact makes most of this module redundant rather than wrong: the noise floor of a
    simulator's cycle count comes out at exactly 1.0, so the lower bound equals the median and
    nothing is discarded as noise. Paying for the statistics in that case is the price of one
    interface instead of two.
    """
    if not shapes:
        return SpeedResult(0.0, 0.0, 0.0, usable=False, note="no shape was given to time")

    if samples < 2 or warmups < 0:
        raise ValueError("at least two sample pairs and non-negative warmups are required")
    per_shape, floors, evidence, medians = {}, [], {}, {}
    for shape in shapes:
        record = {"warmup_pairs": warmups, "samples": [], "self_samples": []}
        evidence[shape] = record
        try:
            _paired_ratios(reference, candidate, lambda i: draw_probe(shape, i), warmups)
            ratios = _paired_ratios(reference, candidate,
                                    lambda i: draw_probe(shape, i + warmups), samples,
                                    record["samples"])
            self_ratios = _paired_ratios(reference, reference,
                                         lambda i: draw_probe(shape, i + warmups), samples,
                                         record["self_samples"])
        except Exception as exc:
            return SpeedResult(0.0, 0.0, 0.0, usable=False, evidence=evidence,
                               note="timing failed on %r: %s" % (shape, exc))
        lower = _bootstrap_lower_bound(ratios)
        upper = 1.0 / _bootstrap_lower_bound([1.0 / r for r in ratios])
        self_lower = _bootstrap_lower_bound(self_ratios)
        self_upper = 1.0 / _bootstrap_lower_bound([1.0 / r for r in self_ratios])
        noise = max(1.0, self_upper, 1.0 / self_lower)
        # Uncertain differences are neutral; an established slowdown must never become a gain.
        adjusted = lower if lower > noise else (upper if upper < 1.0 / noise else 1.0)
        per_shape[shape] = adjusted
        medians[shape] = statistics.median(ratios)
        floors.append(noise)
        record.update(lower=lower, upper=upper, noise_floor=noise, speedup=adjusted)

    worst_shape = min(per_shape, key=lambda s: per_shape[s])
    speedup = per_shape[worst_shape]
    noise = max(floors) if floors else 1.0
    if speedup == 1.0:
        # REPORTED AS 1.0, not as the raw ratio. `speedup` is the field a score is computed from, so
        # leaving 1.03x in it and setting a flag would mean every downstream caller has to remember
        # to re-check the flag -- and the one that forgets pays for noise. The measured value is
        # kept in `measured` for a reader who wants to see what was rejected.
        return SpeedResult(1.0, statistics.median(medians.values()), noise, per_shape,
                           within_noise=True, measured=medians[worst_shape], evidence=evidence,
                           note=("the measured gain (%.3fx on the worst shape, %r) does not clear "
                                 "this machine's own noise (%.3fx); reported as 1.0x, which is what "
                                 "was actually established" % (speedup, worst_shape, noise)))
    return SpeedResult(speedup, statistics.median(medians.values()), noise, per_shape,
                       evidence=evidence, note="worst shape %r of %d" % (worst_shape, len(per_shape)))


def standalone_source() -> str:
    """Embed this exact protocol in emitted verifiers without a runtime FRF dependency."""
    from pathlib import Path
    return Path(__file__).read_text(encoding="utf-8").replace(
        "from __future__ import annotations", "")
