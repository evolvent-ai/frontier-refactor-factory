"""The shared stages, as the call seam implements them.

The pipeline drives six stages and knows what none of them do. This file is the call seam's half of
that bargain: freeze, adequacy, battery, emit and replay, written once and used by every scale that
observes through a wire -- kernel, module and package alike.

WHY THE STAGES LIVE WITH THE SEAM AND NOT WITH THE SCALE. A package task and a module task share
every one of these, and a repo task shares none of them. Grouping by scale would give three copies
of this file; grouping by seam gives one, which is the actual structure of the problem.

WHAT IS SPECIFIC TO THIS SEAM, and it is only two things:

    an observation is a RETURNED VALUE, so instability is handled by discarding the whole probe
    rather than by masking a position -- there is no position in a tree

    every observation is by construction the result of calling the subject, which is why E5 is
    not applicable here and says so rather than being quietly omitted

Everything else -- five runs, the floors, the battery, the emitted layout -- is the pipeline's, and
is reached through the same interfaces the process seam will use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from ...core import adequacy, evidence, harbor, statement
from ...core.scale import Spec
from . import observation as obs


@dataclass
class Corpus:
    """What a freeze produced, in the shape the pipeline's gates read.

    The pipeline reads four attributes and nothing else, which is what keeps it ignorant of the
    seam: how many probes survived, how many points they are worth, how much was lost, and whether
    that loss is survivable.
    """

    expectations: list = field(default_factory=list)
    inputs: dict = field(default_factory=dict)
    discard_rate: float = 0.0
    usable: bool = True
    adequacy_note: str = ""
    adequacy: dict = field(default_factory=dict)
    timed: list = field(default_factory=list)

    @property
    def probes(self) -> int:
        return len(self.expectations)

    @property
    def graded_points(self) -> int:
        # One point per graded probe. A call returns one value, so there is nothing to subdivide --
        # unlike the process seam, where one step is worth four.
        return sum(1 for e in self.expectations if e.graded())


def freeze(spec: Spec, observer, source, *, runs: int) -> Corpus:
    """Run every probe `runs` times and keep only what the subject reproduced exactly.

    THE TIMED PROBES ARE HELD OUT of the graded set. Correctness runs first over everything graded,
    so a probe that is both graded and timed can be memoised: answer it correctly during grading,
    cache what came back, and replay the cache when the clock starts. Held out, the timed inputs are
    new to the submission at the moment it is timed.
    """
    inputs = {"probe-%04d" % i: args for i, args in enumerate(source.draw(source.count))}
    observed: dict = {probe_id: [] for probe_id in inputs}

    for _ in range(runs):
        with observer.subject(spec) as subject:
            for probe_id, args in inputs.items():
                observed[probe_id].append(subject.call("run", args))

    report = obs.freeze_corpus(observed)
    timed = _pick_timed(report.expectations)
    graded = [e for e in report.expectations if e.probe_id not in set(timed)]

    return Corpus(expectations=graded, inputs=inputs, discard_rate=report.discard_rate,
                  usable=report.usable, timed=timed)


def _pick_timed(expectations: list, count: int = 3) -> list:
    """Which probes become the timing workload.

    Taken from the end rather than the start only to be deterministic; nothing here predicts which
    probe is heavy. Choosing by measured cost would be a headroom prediction, and the design says
    that is the solver's discovery rather than ours -- we promise only that the clock can read the
    workload, never that the subject can be made faster on it.
    """
    return [e.probe_id for e in expectations[-count:]] if len(expectations) > count else []


def audit(spec: Spec, observer, corpus: Corpus) -> Corpus:
    """Measure reach and floor, and refuse a corpus that grades mostly constants.

    Reach needs a per-language backend and most languages have none; that is an absence rather than
    a failure, so it is reported and the floor carries the verdict alone.
    """
    reach = observer.coverage().measure(spec, corpus.inputs)
    floor = adequacy.measure_floor(
        lambda name: _score_trivial(observer, spec, corpus, name),
        ("returns-null", "returns-zero", "echoes-the-input"))

    report = adequacy.assess(reach, floor)
    corpus.adequacy = report.to_json()
    corpus.adequacy_note = report.note
    if not report.ok:
        corpus.usable = False
    return corpus


def _score_trivial(observer, spec: Spec, corpus: Corpus, kind: str) -> tuple[int, int]:
    """What a submission that implements nothing scores. Used only to measure the floor."""
    constant = {"returns-null": None, "returns-zero": 0, "echoes-the-input": "echo"}[kind]
    passed = total = 0
    for expectation in corpus.expectations:
        args = corpus.inputs[expectation.probe_id]
        answer = obs.Observation(True, args[0] if constant == "echo" and args else constant)
        got, want, _ = obs.grade(expectation, answer)
        passed += got
        total += want
    return passed, total


def battery(spec: Spec, observer, corpus: Corpus) -> evidence.Battery:
    """The evidence checks, in the order that fails cheapest first."""
    checks = evidence.Battery()
    checks.record(evidence.ceiling(lambda: _score_reference(observer, spec, corpus)))
    checks.record(evidence.floor(
        lambda name: _score_trivial(observer, spec, corpus, name),
        ["returns-null", "returns-zero", "echoes-the-input"]))
    checks.record(evidence.channels_bite(
        lambda channel: _perturb(observer, spec, corpus), ["value"]))
    # E5 does not apply here, and the reason is structural rather than convenient -- see the module
    # docstring. Recording it as NOT_APPLICABLE rather than omitting it keeps the battery's shape
    # identical across seams, so a missing check is visible as a missing check.
    checks.record(evidence.points_are_about_the_subject(lambda: (0, 0), applies=False))
    checks.record(evidence.cannot_delegate_to_the_reference(
        lambda: observer.forbidden_references(spec), lambda: observer.isolated()))
    return checks


def _score_reference(observer, spec: Spec, corpus: Corpus) -> tuple[int, int]:
    passed = total = 0
    with observer.subject(spec) as subject:
        for expectation in corpus.expectations:
            got, want, _ = obs.grade(expectation, subject.call("run", corpus.inputs[expectation.probe_id]))
            passed += got
            total += want
    return passed, total


def _perturb(observer, spec: Spec, corpus: Corpus) -> tuple[bool, bool]:
    """-> (the observation provably moved, the verifier noticed).

    Both halves, because a mutant scoring full marks is ambiguous: either the verifier is blind, or
    the mutation never reached anything graded. Only comparing the observations tells those apart,
    and a check that cannot is a rubber stamp.
    """
    diverged = caught = False
    with observer.subject(spec, mutated=True) as subject:
        for expectation in corpus.expectations:
            answer = subject.call("run", corpus.inputs[expectation.probe_id])
            if answer.digest() != expectation.digest:
                diverged = True
            got, want, _ = obs.grade(expectation, answer)
            if got != want:
                caught = True
    return diverged, caught


def emit(destination: str, spec: Spec, corpus: Corpus, checks: evidence.Battery,
         *, write_tests: Callable) -> str:
    """Write the task tree, and hand the seam-specific parts to `write_tests`."""
    facts = statement.Facts(
        name=spec.name, scale=spec.scale, description=spec.description,
        source_language=spec.language, target_language=spec.target_language,
        probes=corpus.probes, graded_points=corpus.graded_points,
        freeze_runs=max((e.runs for e in corpus.expectations), default=0),
        channels=("the value the call returned",), timed_workloads=len(corpus.timed),
        forbidden=tuple(spec.environment.get("forbidden", ())))

    package = harbor.Package(
        name=spec.name, scale=spec.scale, description=spec.description,
        instruction=statement.render(facts), source_language=spec.language,
        target_language=spec.target_language,
        provenance={"adequacy": corpus.adequacy, "evidence": checks.to_json(),
                    "discard_rate": round(corpus.discard_rate, 4)})

    path = os.path.join(destination, spec.name)
    harbor.write(path, package)
    write_tests(path, corpus)
    return path


def replay(path: str, *, drive: Callable[[str], tuple[int, int]]) -> tuple[int, int]:
    """Drive the emitted package with the reference it ships. -> (passed, total).

    Deliberately a thin pass-through: the whole value is in WHERE it points -- at what was written,
    not at the tree it was built from -- and the driving itself belongs to whoever knows how to
    start a subject from a package.
    """
    return drive(path)


class Seam:
    """The six shared stages, bound to the SCALE rather than to one observer.

    Handed to `Factory.install_stages`, which is what keeps the pipeline free of any knowledge of
    which seam it is driving.

    THE SCALE, NOT AN OBSERVER, and the distinction cost a real bug. An earlier version took an
    observer object and built it. The pipeline builds through the seam and then freezes through
    `scale.observe()`, so with an observer captured here the two were different objects: one was
    built and the other was frozen, and the second failed inside Popen with an IndexError about an
    empty argv, which says nothing whatever about the cause. A batch is also more than one
    candidate, and an observer captured once would serve the first candidate's subject for the rest
    of the run -- every task after the first describing material it was not built from.

    So the seam asks the scale for its observer at the moment it needs one, and the scale is what
    decides how long that observer lives.
    """

    def __init__(self, scale, *, destination: str = "tasks",
                 write_tests: Callable, drive: Callable) -> None:
        self._scale = scale
        self._destination = destination
        self._write_tests = write_tests
        self._drive = drive

    def stages(self) -> dict:
        return {
            "build": lambda spec: self._scale.observe().build(spec),
            "freeze": lambda spec, observer, source, runs: freeze(spec, observer, source, runs=runs),
            "adequacy": audit,
            "battery": battery,
            "emit": lambda spec, corpus, checks: emit(
                self._destination, spec, corpus, checks, write_tests=self._write_tests),
            "replay": lambda path: replay(path, drive=self._drive),
        }
