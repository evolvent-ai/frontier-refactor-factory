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

import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import evidence, harbor
from .scale import Candidate, Scale, Spec

# How many times the reference is run before anything it did is believed. Measured rather than
# chosen: across a set of already-shipped packages, a third of them had at least one observation
# that a single run would have frozen as reproducible and a second run disagreed with.
FREEZE_RUNS = 5

# The floor a timed workload must clear, per call, for a speed score over it to mean anything.
#
# 10 microseconds, and the number is a property of the CLOCK rather than of any subject. A
# `time.perf_counter` pair costs tens of nanoseconds to read and the scheduler can steal a
# microsecond at any moment, so a call that finishes in under ten of them is measured almost
# entirely as jitter: the ratio that comes back describes the machine, not the program. Above it,
# a real algorithmic change moves the number by more than the noise does.
#
# Deliberately low. This exists to reject the tasks whose speed dimension DOES NOT EXIST -- a
# leap-year test, an integer comparison -- not to demand that every subject be a heavy one. A
# subject at 50 microseconds is thin but honest, and it ships.
MIN_TIMED_SECONDS = 10e-6

# Below this a corpus cannot distinguish a real submission from a lucky one, whatever its evidence
# says. Two numbers because they fail differently: a handful of probes each worth many points, and
# many probes each worth almost nothing, are both too thin.
MIN_PROBES = 5
MIN_GRADED_POINTS = 40

# Checks whose INCONCLUSIVE is a statement about the MATERIAL rather than about this factory.
#
# The default reading of "could not conclude" is that we failed to set something up -- no container,
# so isolation is not in force and the delegation check cannot decide. That is ours. But one check
# is inconclusive for a reason that is entirely the subject's: a mutation that provably changes
# nothing. `minimum = maximum = array[0]` mutated to `array[-1]` returns the same minimum, because
# a minimum over the whole array does not depend on which element seeded it. The mutation was
# applied, the subject was rebuilt, and the observation did not move -- there is nothing to repair
# on our side, and a task whose behaviour cannot be perturbed cannot demonstrate that its verifier
# would notice a wrong submission. That is a refusal, and it belongs to the material.
#
# A set rather than a flag on the Verdict because the reason lives with the pipeline's accounting,
# not with the check: `evidence.py` reports what it found, and what a finding COSTS is decided here.
INCONCLUSIVE_IS_MATERIAL = frozenset({"channels-bite"})


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
    identity: str = ""

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
              freeze_runs: int = FREEZE_RUNS,
              log: Callable[[str], None] = lambda _m: None) -> Refused | Emitted:
    """One candidate -> a task, or the reason there is none.

    ONE CANDIDATE MAY NEVER END A BATCH, and that is what the second clause below is for. In a real
    run most candidates fail: a mined function imports its own package and will not load standing
    alone, a build times out, a subject dies on its third probe. Every one of those is an ordinary
    fact about the material, and letting it propagate turns a twenty-candidate run into a stack
    trace after the fourth -- which reports no yield at all, and looks like the factory is broken
    when it is working exactly as intended.

    THE ATTRIBUTION STILL HAS TO BE HONEST, so the two are not collapsed. A refusal a stage raised
    deliberately carries its own verdict. Anything else is UNCLASSIFIED and counted as OURS: we did
    not anticipate it, and a batch dominated by these has measured our bugs rather than the supply.
    Calling an unknown failure "material" would be the comfortable lie -- the yield would look fine
    and the denominator would be quietly wrong.
    """
    if freeze_runs < 2:
        raise ValueError("freeze_runs must be at least 2")
    log("stage build: start")
    try:
        return _run(scale, candidate, hooks, log, freeze_runs)
    except Stage as refusal:
        log("refused at %s: %s (%s): %s" %
            (refusal.stage, refusal.reason, refusal.fault.value, refusal.detail[:400]))
        return Refused(refusal.stage, refusal.reason, refusal.fault, refusal.detail, candidate.identity)
    except KeyboardInterrupt:
        # An operator stopping a batch is not a candidate failing. Re-raised so that Ctrl-C is not
        # silently recorded as twenty unsuitable packages.
        raise
    except BaseException as unexpected:                        # noqa: BLE001 -- see the docstring
        detail = "%s: %s" % (type(unexpected).__name__, unexpected)
        log("refused at unclassified: %s (factory)" % detail.splitlines()[0][:160])
        return Refused("unclassified", type(unexpected).__name__, Fault.FACTORY, detail[:2000], candidate.identity)


def _run(scale: Scale, candidate: Candidate, hooks: Hooks, log: Callable[[str], None],
         freeze_runs: int) -> Emitted:
    spec = _specify(scale, candidate)
    log("specified %s (%s)" % (spec.name, spec.language))

    # A REFERENCE THAT WILL NOT BUILD IS MATERIAL, NOT A FAULT OF OURS. Real repositories fail to
    # build for reasons that are facts about them -- a pinned dependency that no longer resolves, a
    # toolchain the image does not carry, a generated file the release forgot. Uncaught, every one
    # of those was counted as our bug and dragged `trustworthy` down with it.
    log("stage probes: start")
    try:
        hooks.build(spec)
    except (RuntimeError, OSError) as why:
        raise Stage("build", "reference-will-not-build", Fault.MATERIAL, str(why)[:2000])
    log("built")

    # A SCALE THAT CANNOT DRAW PROBES IS DESCRIBING ITS MATERIAL, NOT FAILING. The repo scale
    # raises here when a repository offers no liftable invocation -- a TUI, a daemon, a tool that
    # only talks to the network. That is a fact about the candidate, and deserves the same verdict
    # an unbuildable checkout gets. Left uncaught it reached the outer handler, was counted as
    # OURS, and set `trustworthy: false` for the whole batch -- so a pond full of unsuitable
    # repositories read as a factory with a bug in it, which is the one thing the fault split
    # exists to tell apart.
    try:
        source = scale.probes(spec)
    except ValueError as why:
        raise Stage("probes", "no-probes-could-be-drawn", Fault.MATERIAL, str(why)[:2000])
    except RuntimeError as why:
        # A generator failure can be a malformed material generator or an unavailable
        # execution backend. Both are candidate/setup failures here, never an unclassified
        # factory crash; the detail remains visible for diagnosis.
        raise Stage("probes", "probe-generator-failed", Fault.MATERIAL, str(why)[:2000])
    # A scale may enrich the immutable Spec with material-derived contract details while drawing
    # probes (repo workloads do this). Carry that replacement into freeze, adequacy and emit.
    spec = getattr(scale, "_spec", spec)
    observer = scale.observe()

    log("stage freeze: start runs=%d probes=%d" % (freeze_runs, getattr(source, "count", 0)))
    try:
        report = hooks.freeze(spec, observer, source, runs=freeze_runs)
    except RuntimeError as why:
        # A repository-owned corpus can contain malformed commands or fixtures. Those are
        # material failures; letting them escape as unclassified makes one bad repo poison the
        # batch trust signal and hides the actionable source diagnosis.
        raise Stage("freeze", "reference-corpus-failed", Fault.MATERIAL, str(why)[:2000])
    _check_corpus(report)
    log("froze %d probe(s), %d point(s), discarded %.0f%%"
        % (report.probes, report.graded_points, 100 * report.discard_rate))

    log("stage adequacy: start")
    report = hooks.adequacy(spec, observer, report)
    log("adequacy: %s" % report.adequacy_note)

    # AND THE VERDICT IS ACTED ON. `adequacy` measures reach and floor and then repairs what it
    # can; a corpus still inadequate after those attempts grades a fraction of the subject, and
    # shipping it means a submission can get most of the program arbitrarily wrong with nothing
    # in the corpus moving. An earlier version logged this line and ignored it, which put tasks
    # reaching 18% of their subject into the output directory alongside honest ones.
    #
    # `usable` unset means the seam does not measure adequacy at all -- an absence, not a finding.
    if getattr(report, "usable", True) is False:
        raise Stage("adequacy", "corpus-does-not-measure-the-subject", Fault.MATERIAL,
                    report.adequacy_note or "the corpus is inadequate after repair")

    # TIMING IS REQUIRED when the corpus declares timed probes. If the corpus has no timed field at
    # all (e.g. a toy scale in tests), the check is skipped rather than blocking every non-standard
    # use. A corpus that explicitly declares timed=[] after a timing pass ran is the failure case.
    timed_probes = getattr(report, "timed", None)
    if timed_probes is not None and not timed_probes:
        raise Stage("timing", "no-timed-workload", Fault.MATERIAL,
                    "the corpus has no workload heavy enough to measure; this is a performance "
                    "benchmark, so timing is required")

    # AND THE WORKLOAD MUST BE HEAVY ENOUGH TO READ. Declaring timed probes is not the same as
    # having a measurable workload: a leap-year test is three integer operations, and the
    # stopwatch over it reads this machine's noise however many probes are held out. Such a task
    # ships an instruction promising "your score rises with the measured speedup" over a
    # measurement that cannot move, so every submission scores exactly 0.5 whatever it does.
    #
    # None means the measurement was never attempted (no `time` op on this seam, or the subject
    # refused the question) -- an absence, not a finding, and it must not refuse the task.
    timed_seconds = getattr(report, "timed_seconds", None)
    if timed_seconds is not None and timed_seconds < MIN_TIMED_SECONDS:
        raise Stage("timing", "workload-too-fast-to-measure", Fault.MATERIAL,
                    "the heaviest timed probe runs in %.2f us, below the %.0f us floor this "
                    "machine's clock can resolve; a speed score over it would be noise"
                    % (timed_seconds * 1e6, MIN_TIMED_SECONDS * 1e6))

    log("stage evidence: start")
    battery = hooks.battery(spec, observer, report)
    if not battery.ok:
        # WHOSE FAULT DEPENDS ON WHICH WAY THE CHECK FAILED, and collapsing the two would make the
        # yield figure meaningless. A check that FAILS has established something about this
        # candidate: its reference cannot reproduce its own expectations, a trivial submission
        # scores full marks. That is the material's fault and a low yield is the honest answer.
        #
        # A check that is INCONCLUSIVE has established nothing at all, and on this pipeline the
        # commonest cause is ours: no container backend, so execution isolation is not in force and
        # the delegation check cannot conclude. Charging that to the material would report a 0%
        # yield for a batch where nothing was wrong with any of the candidates -- which is precisely
        # the confusion DESIGN.md s12.5 exists to prevent.
        # NOT EVERY INCONCLUSIVE IS OURS, and the exception is measured rather than supposed. A
        # subject can be genuinely unperturbable: `minimum = maximum = array[0]` becomes
        # `array[-1]` and the answer is identical, because a minimum over the whole array does not
        # depend on which element seeded it. Nothing is wrong with the factory there and nothing is
        # wrong with the mutation -- the material simply cannot be made to differ this way, which
        # is a fact about the material. On a real batch of twenty this was two of the twenty, and
        # charging it to us put the run on the edge of reporting itself untrustworthy.
        failures = battery.failures()
        undecided = [v for v in failures if v.outcome is evidence.Outcome.INCONCLUSIVE]
        ours = [v for v in undecided if v.check not in INCONCLUSIVE_IS_MATERIAL]
        raise Stage("evidence", failures[0].check,
                    Fault.FACTORY if len(ours) == len(failures) else Fault.MATERIAL,
                    "; ".join(v.detail for v in failures))
    log("evidence: %d check(s) held" % len(battery.verdicts))

    path = hooks.emit(spec, report, battery)

    quality_errors = harbor.deterministic_quality(path)
    if quality_errors:
        raise Stage("emit", "deterministic-quality-failed", Fault.FACTORY,
                    "; ".join(quality_errors))

    # THE LAST GATE OPENS WHAT WAS WRITTEN. Everything above measured the build tree. This drives
    # the emitted package with the reference the package itself ships, which is where a whole class
    # of fault lives -- an artefact built in a scratch directory and never copied, an expectation
    # frozen against a path that is not in the package. Measured on an earlier factory, 14 of 78
    # packages failed exactly this after passing everything else.
    try:
        verdict = evidence.package_reproduces_itself(lambda: hooks.replay(path))
    except RuntimeError as why:
        # A shipped reference that disagrees with its frozen corpus is a material nondeterminism
        # or dependency problem. Keep factory trust intact and let sourcing move on.
        raise Stage("emit", "package-reference-replay-failed", Fault.MATERIAL, str(why)[:2000])
    battery.record(verdict)
    if not verdict.ok:
        raise Stage("emit", "package-does-not-reproduce-itself", Fault.FACTORY, verdict.detail)

    # Purge bytecode left by replay. The E7 step executes the reference Python implementation
    # directly, which causes Python to compile and cache .pyc files into tests/reference/.
    # Those files must not ship: they reveal the Python version used and are not needed for grading.
    _purge_task_bytecode(path)

    log("emitted %s" % path)

    return Emitted(spec.name, path, spec.scale, report.graded_points, report.probes,
                   report.discard_rate, battery.to_json())


def _purge_task_bytecode(task_path: str) -> None:
    """Remove __pycache__ dirs and .pyc/.pyo files from an emitted task tree.

    The E7 replay step executes the reference Python implementation, causing Python to
    compile and write .pyc files into tests/reference/. These must not ship: they reveal
    the Python version used during freeze and serve no purpose for grading.

    Called after replay() completes so it catches everything written during execution.
    """
    for dirpath, dirnames, filenames in os.walk(task_path, topdown=False):
        for name in filenames:
            if name.endswith(".pyc") or name.endswith(".pyo"):
                try:
                    os.remove(os.path.join(dirpath, name))
                except OSError:
                    pass
    for dirpath, dirnames, filenames in os.walk(task_path, topdown=False):
        for name in dirnames:
            if name == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)


def _accepts(function, name: str) -> bool:
    """Whether `function` declares a keyword called `name`.

    Asked rather than assumed, because the alternative -- passing it and catching TypeError -- is
    indistinguishable from the subject raising TypeError for its own reasons.
    """
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _specify(scale: Scale, candidate: Candidate) -> Spec:
    """Ask the scale what to build, and refuse anything that is not a `Spec`.

    The check is here rather than in the scale because the risk is uniform: `specify` is where a
    model's proposal enters the pipeline, and a proposal is only ever allowed to fill in fields of a
    type defined in `core`. It cannot introduce a mechanism, because there is nowhere to put one.

    If the scale carries a `_task_form` attribute (set by automation.run()), it is forwarded as a
    keyword argument so the scale can honour the configured form without the factory needing a new
    API surface.
    """
    try:
        kwargs = {}
        task_form = getattr(scale, "_task_form", None)
        # ADVISORY MEANS ADVISORY. Passed only to a scale whose `specify` actually declares it:
        # the attribute is set on every scale by `automation.run`, and forwarding it blindly meant
        # the three scales that do not take it raised TypeError -- caught below and reported as
        # `could-not-specify (material)`, so a wiring mistake of ours wore the disguise of unusable
        # material and refused every candidate in a batch.
        if task_form is not None and _accepts(scale.specify, "task_form"):
            kwargs["task_form"] = task_form
        spec = scale.specify(candidate, **kwargs)
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
        # A seam may say WHY in its own words. Without that, every refusal here reads as "the
        # reference did not reproduce its probes" -- a specific diagnosis, and the wrong one for a
        # subject that never started, which is the commonest outcome on real mined material.
        given = getattr(report, "unusable_reason", "")
        raise Stage("freeze", "will-not-repeat-itself", Fault.MATERIAL,
                    given or
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
