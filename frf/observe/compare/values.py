"""Deciding whether two returned values are the same answer.

Three strictnesses, and which one applies is a property of the subject rather than a knob to turn
when a task will not pass.

    EXACT      bytes, integers, strings, booleans. Anything that is not identical is wrong.
    STRUCTURAL JSON equivalence: key order is not information, and numbers compare by closeness.
    ENVELOPE   a candidate may be no less accurate than the reference is, and no more.

WHY THE ENVELOPE IS NOT OPTIONAL FOR NUMERICAL WORK. A routine that changes the order it accumulates
in -- which is what vectorising, blocking or parallelising it does -- produces a DIFFERENT last few
bits and is completely correct. Demanding bit-equality there rejects every real optimisation and
accepts only the ones that changed nothing. So the reference's own error is the ruler: computed in
double precision, compared against itself in the precision it ships in, and a candidate is allowed
that much error and no more.

The alternative -- a fixed tolerance like 1e-9 -- fails in both directions at once. It is far too
strict for an ill-conditioned subject where the reference itself is only good to 1e-6, and far too
loose for a well-conditioned one where any real error is a bug.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# How many multiples of the reference's own error a candidate may spend. Three rather than one
# because the reference's error is itself an estimate drawn from a finite sample; a candidate that
# is genuinely as accurate would otherwise fail about as often as the estimate happens to be low.
KAPPA = 3.0

# Floors, for when the reference is exact or nearly so. Without them a subject that happens to be
# bit-exact on one probe would demand bit-exactness of every candidate on that probe, which is the
# strict-comparison failure the envelope exists to avoid.
ABSOLUTE_FLOOR = 1e-9
RELATIVE_FLOOR = 1e-7


@dataclass(frozen=True)
class Verdict:
    same: bool
    detail: str = ""


def _is_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def exact(expected: object, actual: object) -> Verdict:
    """Identity. Used where a difference of any size is a difference in behaviour."""
    if expected == actual and type(expected) is type(actual):
        return Verdict(True)
    return Verdict(False, "expected %r, got %r" % (_clip(expected), _clip(actual)))


def structural(expected: object, actual: object, *, rel: float = 1e-9,
               abs_: float = 1e-12) -> Verdict:
    """JSON equivalence: key order is not information, numbers compare by closeness.

    Lists stay ordered, because order in a returned sequence usually IS the answer. Booleans are
    never equal to numbers, however JSON might blur them -- `True == 1` in Python, and a subject
    returning one where the reference returned the other has changed its behaviour.
    """
    return _structural(expected, actual, rel, abs_, path="$")


def _structural(expected: object, actual: object, rel: float, abs_: float, path: str) -> Verdict:
    if isinstance(expected, bool) or isinstance(actual, bool):
        if expected is actual:
            return Verdict(True)
        return Verdict(False, "%s: expected %r, got %r" % (path, expected, actual))
    if _is_number(expected) and _is_number(actual):
        if math.isclose(expected, actual, rel_tol=rel, abs_tol=abs_):
            return Verdict(True)
        return Verdict(False, "%s: expected %r, got %r" % (path, expected, actual))
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            missing = sorted(set(expected) - set(actual))[:3]
            extra = sorted(set(actual) - set(expected))[:3]
            return Verdict(False, "%s: keys differ (missing %s, unexpected %s)"
                                  % (path, missing, extra))
        for key in expected:
            verdict = _structural(expected[key], actual[key], rel, abs_, "%s.%s" % (path, key))
            if not verdict.same:
                return verdict
        return Verdict(True)
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return Verdict(False, "%s: expected %d element(s), got %d"
                                  % (path, len(expected), len(actual)))
        for i, (e, a) in enumerate(zip(expected, actual)):
            verdict = _structural(e, a, rel, abs_, "%s[%d]" % (path, i))
            if not verdict.same:
                return verdict
        return Verdict(True)
    if type(expected) is not type(actual):
        return Verdict(False, "%s: expected %s, got %s"
                              % (path, type(expected).__name__, type(actual).__name__))
    return exact(expected, actual) if expected != actual else Verdict(True)


def envelope(expected: object, actual: object, reference_error: float, *,
             kappa: float = KAPPA) -> Verdict:
    """Numerical equality, measured against how accurate the reference itself is.

    `reference_error` is the reference's own relative error -- how far it lands from itself computed
    in higher precision. A candidate within `kappa` times that has not lost anything the reference
    was not already losing; beyond it, the candidate is doing arithmetic the reference does not.
    """
    allowed = max(kappa * abs(reference_error), RELATIVE_FLOOR)
    return _envelope(expected, actual, allowed, path="$")


def _envelope(expected: object, actual: object, allowed: float, path: str) -> Verdict:
    if _is_number(expected) and _is_number(actual):
        if not math.isfinite(expected) or not math.isfinite(actual):
            # A NaN is not close to anything, including itself. Two NaNs in the same place ARE the
            # same behaviour, though, and a subject that legitimately produces one stays gradeable.
            if math.isnan(expected) and math.isnan(actual):
                return Verdict(True)
            if expected == actual:
                return Verdict(True)
            return Verdict(False, "%s: expected %r, got %r" % (path, expected, actual))
        scale = max(abs(expected), 1.0)
        if abs(expected - actual) <= allowed * scale + ABSOLUTE_FLOOR:
            return Verdict(True)
        return Verdict(False, "%s: expected %.12g, got %.12g (allowed %.3g relative)"
                              % (path, expected, actual, allowed))
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return Verdict(False, "%s: expected %d element(s), got %d"
                                  % (path, len(expected), len(actual)))
        for i, (e, a) in enumerate(zip(expected, actual)):
            verdict = _envelope(e, a, allowed, "%s[%d]" % (path, i))
            if not verdict.same:
                return verdict
        return Verdict(True)
    # Anything not numeric inside a numeric result is structure, and structure is exact.
    return structural(expected, actual)


def _clip(value: object, limit: int = 120) -> object:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."
