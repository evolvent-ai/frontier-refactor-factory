"""The eight stages, driven once for every scale.

    SOURCE -> SPECIFY -> BUILD -> PROBE -> FREEZE -> ADEQUACY -> EVIDENCE -> EMIT

This module knows the ORDER and the GATES. It does not know what an observation is, what a probe
looks like, or which index the material came from -- those belong to the seam and the scale, and
keeping them out is what lets one file serve four scales.

WHY A RESULT RATHER THAN AN EXCEPTION. A candidate that cannot become a task is the normal case, not
an error: at repo scale roughly nine in ten are refused. The caller is running a batch, so a refusal
has to be a value it can count and route on, and the reason has to be precise enough to answer the
question that decides everything else --

    WAS THIS THE MATERIAL, OR WAS THIS US?

A failure attributed to the wrong side is worse than no attribution: it sends the repair loop to fix
the material when the factory is broken, and the yield number then measures our own bugs. Every
`Refused` carries which of the two it was, and the split is reported.

WHAT ORDER BUYS. The stages are cheapest-and-most-decisive first. There is no point auditing the
adequacy of a corpus whose reference cannot reproduce it, and no point running an evidence battery
on a task that has no corpus. Each gate below is placed at the earliest point where its question can
be answered at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import evidence
from .scale import Candidate, Scale, Spec

# How many times the reference is run before anything it did is believed. Measured rather than
# chosen: across a set of already-shipped packages, a third of them had at least one observation
# that a single run would have frozen as reproducible and a second run disagreed with.
FREEZE_RUNS = 5

# Below this a corpus cannot distinguish a real submission from a lucky one, whatever its evidence
# says. Two numbers because they fail differently: a handful of probes each worth many points, and
# many probes each worth almost nothing, are both too thin.
MIN_PROBES = 5
MIN_GRADED_POINTS = 40


class Fault(Enum):
    """Whose problem a refusal was. The single most important field in this module.

    MATERIAL is the normal outcome and needs no repair -- this repository, package or function was
    not suitable. FACTORY means we broke, and a batch where FACTORY dominates has not measured a
    yield at all; it has measured our own bugs and called them a property of the material.
    """

    MATERIAL = "material"
    FACTORY = "factory"


@dataclass(frozen=True)
class Refused:
    stage: str
    reason: str
    fault: Fault
    detail: str = ""

    ok = False

    def to_json(self) -> dict:
        return {"ok": False, "stage": self.stage, "reason": self.reason,
                "fault": self.fault.value, "detail": self.detail[:2000]}


@dataclass(frozen=True)
class Emitted:
    name: str
    path: str
    scale: str
    graded_points: int
    probes: int
    discard_rate: float
    battery: list = field(default_factory=list)

    ok = True

    def to_json(self) -> dict:
        return {"ok": True, "name": self.name, "path": self.path, "scale": self.scale,
                "graded_points": self.graded_points, "probes": self.probes,
                "discard_rate": round(self.discard_rate, 4), "battery": self.battery}


class Stage(Exception):
    """Raised inside a stage to refuse a candidate. Carries the attribution with it.

    An exception rather than a return value only INSIDE this module: the stages are a straight line
    and threading a result through eight of them would bury the line in checks. It never escapes --
    `build_one` turns it into a `Refused`.
    """

    def __init__(self, stage: str, reason: str, fault: Fault, detail: str = "") -> None:
        super().__init__("%s/%s" % (stage, reason))
        self.stage, self.reason, self.fault, self.detail = stage, reason, fault, detail


@dataclass
class Hooks:
    """The parts of a run that differ by seam or by deployment, injected rather than branched on.

    This is the seam between "the order of the stages" and "what the stages do". The pipeline calls
    these; it does not know whether `freeze` masked a line or discarded a probe, and it must not.
    """

    build: Callable
    freeze: Callable
    adequacy: Callable
    battery: Callable
    emit: Callable
    replay: Callable


def build_one(scale: Scale, candidate: Candidate, hooks: Hooks, *,
              log: Callable[[str], None] = lambda _m: None) -> Refused | Emitted:
    """One candidate -> a task, or the reason there is none."""
    try:
        return _run(scale, candidate, hooks, log)
    except Stage as refusal:
        log("refused at %s: %s (%s)" % (refusal.stage, refusal.reason, refusal.fault.value))
        return Refused(refusal.stage, refusal.reason, refusal.fault, refusal.detail)


def _run(scale: Scale, candidate: Candidate, hooks: Hooks, log: Callable[[str], None]) -> Emitted:
    spec = _specify(scale, candidate)
    log("specified %s (%s)" % (spec.name, spec.language))

    hooks.build(spec)
    log("built")

    source = scale.probes(spec)
    observer = scale.observe()

    report = hooks.freeze(spec, observer, source, runs=FREEZE_RUNS)
    _check_corpus(report)
    log("froze %d probe(s), %d point(s), discarded %.0f%%"
        % (report.probes, report.graded_points, 100 * report.discard_rate))

    report = hooks.adequacy(spec, observer, report)
    log("adequacy: %s" % report.adequacy_note)

    battery = hooks.battery(spec, observer, report)
    if not battery.ok:
        raise Stage("evidence", battery.failures()[0].check, Fault.MATERIAL,
                    "; ".join(v.detail for v in battery.failures()))
    log("evidence: %d check(s) held" % len(battery.verdicts))

    path = hooks.emit(spec, report, battery)

    # THE LAST GATE OPENS WHAT WAS WRITTEN. Everything above measured the build tree. This drives
    # the emitted package with the reference the package itself ships, which is where a whole class
    # of fault lives -- an artefact built in a scratch directory and never copied, an expectation
    # frozen against a path that is not in the package. Measured on an earlier factory, 14 of 78
    # packages failed exactly this after passing everything else.
    verdict = evidence.package_reproduces_itself(lambda: hooks.replay(path))
    battery.record(verdict)
    if not verdict.ok:
        raise Stage("emit", "package-does-not-reproduce-itself", Fault.FACTORY, verdict.detail)
    log("emitted %s" % path)

    return Emitted(spec.name, path, spec.scale, report.graded_points, report.probes,
                   report.discard_rate, battery.to_json())


def _specify(scale: Scale, candidate: Candidate) -> Spec:
    """Ask the scale what to build, and refuse anything that is not a `Spec`.

    The check is here rather than in the scale because the risk is uniform: `specify` is where a
    model's proposal enters the pipeline, and a proposal is only ever allowed to fill in fields of a
    type defined in `core`. It cannot introduce a mechanism, because there is nowhere to put one.
    """
    try:
        spec = scale.specify(candidate)
    except Exception as exc:                                   # noqa: BLE001 -- reported, not raised
        raise Stage("specify", "could-not-specify", Fault.MATERIAL,
                    "%s: %s" % (type(exc).__name__, exc))
    if not isinstance(spec, Spec):
        raise Stage("specify", "not-a-spec", Fault.FACTORY,
                    "%s.specify returned %s" % (type(scale).__name__, type(spec).__name__))
    if not spec.invoke:
        raise Stage("specify", "nothing-to-invoke", Fault.MATERIAL,
                    "the specification names no way to start the subject")
    return spec


def _check_corpus(report) -> None:
    """The two floors, and the discard rate that decides whether the subject is usable at all."""
    if not report.usable:
        raise Stage("freeze", "will-not-repeat-itself", Fault.MATERIAL,
                    "%.0f%% of probes were discarded because the reference did not reproduce them; "
                    "what survives was selected by luck rather than by being representative"
                    % (100 * report.discard_rate))
    if report.probes < MIN_PROBES or report.graded_points < MIN_GRADED_POINTS:
        raise Stage("freeze", "corpus-too-thin", Fault.MATERIAL,
                    "%d probe(s) and %d graded point(s); a corpus this small cannot tell a correct "
                    "submission from a lucky one (need >= %d and >= %d)"
                    % (report.probes, report.graded_points, MIN_PROBES, MIN_GRADED_POINTS))


@dataclass
class Batch:
    """What a run of many candidates produced, and where the failures fell.

    The split by fault is the number that says whether a low yield is a fact about the material or a
    bug in here. It is computed rather than eyeballed because it is easy to read a list of failures
    and conclude what one already believed.
    """

    emitted: list = field(default_factory=list)
    refused: list = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return len(self.emitted) + len(self.refused)

    @property
    def yield_rate(self) -> float:
        return (len(self.emitted) / self.attempted) if self.attempted else 0.0

    @property
    def our_fault(self) -> int:
        return sum(1 for r in self.refused if r.fault is Fault.FACTORY)

    @property
    def trustworthy(self) -> bool:
        """A yield is only a measurement of the material if the factory was mostly not the problem."""
        return self.attempted > 0 and self.our_fault <= self.attempted // 10

    def summary(self) -> dict:
        by_reason: dict = {}
        for refusal in self.refused:
            key = "%s/%s" % (refusal.stage, refusal.reason)
            by_reason[key] = by_reason.get(key, 0) + 1
        return {"attempted": self.attempted, "emitted": len(self.emitted),
                "yield_rate": round(self.yield_rate, 4),
                "refused_material": len(self.refused) - self.our_fault,
                "refused_factory": self.our_fault,
                "trustworthy": self.trustworthy,
                "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1]))}
