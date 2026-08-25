"""What one call produced, and how N of them become an Expectation.

An observation on this seam is a RETURNED VALUE -- a tree of JSON -- or the refusal that came
instead. That shape decides how instability is handled, and it decides it differently from the
process seam, which is why the two freezes live apart.

    process seam:  an ordered sequence of LINES.  Line 7 varies every run -> exclude line 7, grade
                   the rest. The line number is a stable coordinate, so a hole in the record has a
                   meaning that survives.

    this seam:     a TREE. There is no line 7. Flattening it to invent one would make a masked
                   position point at a different field the moment key order changed, and the mask
                   would then be hiding whatever moved into the slot.

So here an unstable probe is DISCARDED WHOLE, and the discard rate is reported. Losing a few probes
costs a little corpus; inventing a coordinate system costs correctness, silently. And a subject that
loses most of its probes should be refused outright rather than quietly shrunk down to the handful
that happened to agree -- a corpus small enough to survive by luck cannot distinguish anything.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Observation:
    """One call's outcome: a value, or a refusal.

    `error` is a first-class outcome rather than an absence. How a subject rejects bad input is part
    of what a reimplementation has to reproduce, so "raised ValueError" is an answer to be compared,
    not a failure to record one.
    """

    ok: bool
    value: Any = None
    error: str = ""

    def digest(self) -> str:
        """A stable fingerprint. Key order must not change it -- two dicts built differently but
        equal in content are the same observation, and JSON gives no guarantee about ordering."""
        error = self.error
        if error in {"ValueError: max() iterable argument is empty",
                     "ValueError: max() arg is an empty sequence"}:
            error = "ValueError: max() arg is an empty sequence"
        body = json.dumps({"ok": self.ok, "value": self.value, "error": error},
                          sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    def to_json(self) -> dict:
        return {"ok": self.ok, "value": self.value, "error": self.error}

    @classmethod
    def from_json(cls, data: dict) -> "Observation":
        return cls(ok=bool(data.get("ok", False)), value=data.get("value"),
                   error=str(data.get("error", "")))


@dataclass(frozen=True)
class Expectation:
    """What the reference reproducibly does for one probe.

    Only the digest is kept, never the value. A verifier's own directory is readable by nothing
    in this design, but the rule is cheaper to keep than to check: an expectation storing the
    answer in plaintext is one filesystem mistake from being a key a submission can replay.
    """

    probe_id: str
    digest: str
    runs: int
    dropped: bool = False
    drop_reason: str = ""

    def graded(self) -> bool:
        return not self.dropped

    def to_json(self) -> dict:
        return {"probe_id": self.probe_id, "digest": self.digest, "runs": self.runs,
                "dropped": self.dropped, "drop_reason": self.drop_reason}

    @classmethod
    def from_json(cls, data: dict) -> "Expectation":
        return cls(probe_id=str(data["probe_id"]), digest=str(data["digest"]),
                   runs=int(data.get("runs", 0)), dropped=bool(data.get("dropped", False)),
                   drop_reason=str(data.get("drop_reason", "")))


def freeze(probe_id: str, runs: list[Observation]) -> Expectation:
    """N observations of one probe -> what may be graded.

    Unanimity or nothing. Anything short of every run agreeing means the reference does not actually
    repeat this, and grading it would fail correct submissions at random -- which is worse than not
    grading it, because it ships looking sound and then misjudges people.
    """
    if not runs:
        return Expectation(probe_id, "", 0, dropped=True, drop_reason="the reference never ran")
    digests = {r.digest() for r in runs}
    if len(digests) > 1:
        return Expectation(probe_id, "", len(runs), dropped=True,
                           drop_reason="the reference gave %d different answers across %d runs"
                                       % (len(digests), len(runs)))
    return Expectation(probe_id, digests.pop(), len(runs))


# How much of a corpus may be lost to instability before the subject itself is the problem. Not a
# tuning knob: past this point what survives is the handful of probes that happened to agree, and a
# corpus selected by luck cannot be trusted to distinguish anything.
MAX_DISCARD_FRACTION = 0.25


@dataclass(frozen=True)
class FreezeReport:
    """What a whole corpus's freeze produced, including what it lost.

    The loss is the point. Discarding unstable probes silently would let a subject that is mostly
    nondeterministic ship as a small tidy task, and the number that reveals it -- how much had to be
    thrown away -- is exactly the one a quiet implementation never records.
    """

    expectations: list[Expectation]
    discarded: list[Expectation] = None

    def __post_init__(self) -> None:
        if self.discarded is None:
            object.__setattr__(self, "discarded", [])

    @property
    def attempted(self) -> int:
        return len(self.expectations) + len(self.discarded)

    @property
    def discard_rate(self) -> float:
        return (len(self.discarded) / self.attempted) if self.attempted else 0.0

    @property
    def usable(self) -> bool:
        """False means REJECT THE SUBJECT, not "carry on with a smaller corpus"."""
        return bool(self.expectations) and self.discard_rate <= MAX_DISCARD_FRACTION

    def to_json(self) -> dict:
        return {"graded": len(self.expectations), "discarded": len(self.discarded),
                "attempted": self.attempted, "discard_rate": round(self.discard_rate, 4),
                "usable": self.usable,
                "reasons": sorted({e.drop_reason for e in self.discarded})[:5]}


def freeze_corpus(runs_by_probe: dict) -> FreezeReport:
    """{probe_id: [Observation, ...]} -> the gradeable expectations, and the loss."""
    kept, lost = [], []
    for probe_id, runs in runs_by_probe.items():
        expectation = freeze(probe_id, runs)
        (kept if expectation.graded() else lost).append(expectation)
    return FreezeReport(kept, lost)


def grade(expectation: Expectation, actual: Observation) -> tuple[int, int, str]:
    """-> (passed, total, reason). An ungraded expectation contributes nothing to either count."""
    if not expectation.graded():
        return 0, 0, ""
    if actual.digest() == expectation.digest:
        return 1, 1, ""
    # WHAT differed, in the terms the two outcomes are in. "expected a value, got a refusal" is a
    # different bug from "both refused, differently", and a solver reading the report needs to know
    # which -- the first is a missing feature, the second is a wrong message.
    if actual.ok:
        detail = "expected the frozen outcome, got a value"
    else:
        detail = "the call was refused: %s" % (actual.error[:200] or "(no message)")
    return 0, 1, detail
