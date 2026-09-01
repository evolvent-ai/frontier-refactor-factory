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
import time
from dataclasses import dataclass, field
from typing import Callable

from ...core import adequacy, evidence, harbor, statement
from ...core.capabilities import capability
from ...core.scale import Spec
from . import observation as obs
from .runner import SubjectFailed, SubjectUnreachable


# How many DIFFERENT perturbations E3 may try before concluding that a subject cannot be
# distinguished this way. More than one because an edit can be real and still semantically inert:
# `find_min_max` initialises from `array[0]`, and rewriting that to `array[-1]` computes exactly the
# same answer. One such edit should not decide the verdict for a subject that is otherwise
# perfectly gradeable.
#
# Declared HERE and not imported from a scale: this is the seam, and a seam that reaches into
# `scales/` has inverted the dependency the whole layout exists to keep.
MUTATION_ATTEMPTS = 4


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
    # Why the corpus is unusable, when the reason is not the discard rate. Without it every refusal
    # at this stage reads "the reference did not reproduce its probes", which is a specific and
    # wrong diagnosis for a subject that never started at all.
    unusable_reason: str = ""
    # Whether that reason is OURS rather than the material's. A mute subject is ordinarily the
    # candidate's doing, so freeze charges it to the material -- but a sandbox that vanished
    # mid-push taught us nothing about the candidate, and filing it as bad material inflates the
    # refusal record with our own outages.
    unusable_is_ours: bool = False
    # Whether the freeze ran out of TIME rather than out of agreement. Still the material's fault --
    # a subject too slow to answer inside the budget cannot be graded, which is the same verdict
    # `PROBE_TIMEOUT` already makes -- but a different finding, and the ledger should say which.
    # Without this a timeout is filed as `will-not-repeat-itself`, which asserts that the reference
    # disagreed with itself. It did not; it never finished being asked.
    unusable_is_timeout: bool = False
    adequacy_note: str = ""
    adequacy: dict = field(default_factory=dict)
    timed: list = field(default_factory=list)

    @property
    def probes(self) -> int:
        return len(self.expectations)

    @property
    def runs(self) -> int:
        """How many repeats this was actually distilled from, as the process seam also reports.

        DERIVED RATHER THAN STORED, because on this seam it is a fact about the expectations: a
        freeze that was cut short leaves probes that saw fewer runs, and the configured number would
        be claiming evidence nobody collected. Exposed under the same name the other seam uses so a
        reader of a corpus does not have to know which seam produced it -- quoting the configured
        count once already shipped a task whose provenance said "0 runs" beside a statement that
        correctly said five.
        """
        return max((e.runs for e in self.expectations), default=0)

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
    try:
        # STRICTLY INSIDE THE SANDBOX THAT HOLDS IT. E2B refuses a lifetime over an hour, so the
        # sandbox can be at most 3600s and this default was exactly that -- a freeze allowed to run
        # until the moment its own sandbox expires, which is a freeze that gets killed rather than
        # finished. The ceiling moved, so this has to move further.
        max_seconds = float(os.environ.get("FRF_FREEZE_MAX_SECONDS", "3000"))
    except ValueError:
        max_seconds = 3600.0
    deadline = time.monotonic() + max_seconds

    try:
        for run_index in range(runs):
            # CHECKED HERE TOO, and this is the branch that needed it. The deadline below sits
            # inside the per-probe loop, which the batched path skips entirely with its `continue`
            # -- so on the remote seam, the path EVERY sandboxed batch takes, the freeze budget was
            # declared and never applied. Five runs of a subject that answers slowly could then run
            # for hours against a stated hour, and the batch's only real bound was the wrapper
            # around the whole process.
            if time.monotonic() >= deadline:
                return Corpus(inputs=inputs, discard_rate=1.0, usable=False,
                              unusable_is_timeout=True,
                              unusable_reason="freeze timeout after %.0fs" % max_seconds)
            with observer.subject(spec) as subject:
                items = list(inputs.items())
                if hasattr(subject, "call_many"):
                    # Remote call seams can batch bounded JSONL requests, avoiding one E2B
                    # process round-trip per package operation while preserving response order.
                    answers = subject.call_many("entry", [args for _, args in items])
                    for (probe_id, _), answer in zip(items, answers.values()):
                        observed[probe_id].append(answer)
                    continue
                for probe_index, (probe_id, args) in enumerate(items, 1):
                    if probe_index == 1 or probe_index % 10 == 0 or probe_index == len(inputs):
                        print("[freeze] run %d/%d probe %d/%d" %
                              (run_index + 1, runs, probe_index, len(inputs)), flush=True)
                    if time.monotonic() >= deadline:
                        return Corpus(inputs=inputs, discard_rate=1.0, usable=False,
                                      unusable_is_timeout=True,
                                      unusable_reason="freeze timeout after %.0fs" % max_seconds)
                    observed[probe_id].append(subject.call("run", args))
    except SubjectFailed as failure:
        # A SUBJECT THAT WILL NOT SPEAK IS THE MATERIAL'S FAULT, and reporting it through `usable`
        # is the whole reason this is caught. The commonest cause by far is a mined function whose
        # file imports its own package -- `from .core import x` resolves inside the distribution and
        # not beside a shim, which is an ordinary fact about real source rather than a defect in
        # this factory. Left uncaught it arrives at `build_one` as an unclassified failure counted
        # as OURS, and a batch of twenty then computes a yield against a denominator of our own bugs.
        #
        # EXCEPT WHEN WE NEVER REACHED IT. `SubjectUnreachable` means the sandbox died or the upload
        # failed, so nothing was learned about this candidate; charging that to the material inflates
        # the refusal record with our own outages, which is the one direction the record must not
        # err in -- a padded refusal log reads as converged.
        return Corpus(inputs=inputs, discard_rate=1.0, usable=False,
                      unusable_is_ours=isinstance(failure, SubjectUnreachable),
                      unusable_reason="the subject never answered: %s" % str(failure)[:600])

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
        ids = [e.probe_id for e in corpus.expectations]
        # Replay in the same one-request order used by freeze. Batching changes stateful subjects'
        # call grouping and can make a reproducible reference miss its own expectations.
        actual = [subject.call("run", corpus.inputs[pid]) for pid in ids]
        for expectation, answer in zip(corpus.expectations, actual):
            got, want, _ = obs.grade(expectation, answer)
            passed += got
            total += want
    return passed, total


def _perturb(observer, spec: Spec, corpus: Corpus) -> tuple[bool, bool]:
    """-> (the observation provably moved, the verifier noticed).

    Both halves, because a mutant scoring full marks is ambiguous: either the verifier is blind, or
    the mutation never reached anything graded. Only comparing the observations tells those apart,
    and a check that cannot is a rubber stamp.
    """
    for attempt in range(MUTATION_ATTEMPTS):
        diverged = caught = False
        try:
            with observer.subject(spec, mutated=True, attempt=attempt) as subject:
                ids = [e.probe_id for e in corpus.expectations]
                inputs = [corpus.inputs[pid] for pid in ids]
                if hasattr(subject, "call_many"):
                    replies = subject.call_many("run", inputs)
                    actual = [replies[rid] for rid in sorted(replies)]
                else:
                    actual = [subject.call("run", args) for args in inputs]
                for expectation, answer in zip(corpus.expectations, actual):
                    if answer.digest() != expectation.digest:
                        diverged = True
                    got, want, _ = obs.grade(expectation, answer)
                    if got != want:
                        caught = True
        except TypeError:
            # An observer that does not accept `attempt` only has one mutant to offer, which is the
            # older contract and still a valid answer -- there is simply nothing further to try.
            with observer.subject(spec, mutated=True) as subject:
                for expectation in corpus.expectations:
                    answer = subject.call("run", corpus.inputs[expectation.probe_id])
                    if answer.digest() != expectation.digest:
                        diverged = True
                    got, want, _ = obs.grade(expectation, answer)
                    if got != want:
                        caught = True
            return diverged, caught
        if diverged:
            # A perturbation that MOVED the observation is what the check needs; whether it was
            # caught is the finding. Stopping here rather than trying the rest keeps the cost of a
            # gradeable subject at one mutant.
            return diverged, caught
    # Nothing this crude could distinguish. Reported as no divergence, which E3 turns into
    # INCONCLUSIVE -- the honest verdict for a subject nobody could perturb this way.
    return False, False


def emit(destination: str, spec: Spec, corpus: Corpus, checks: evidence.Battery,
         *, write_tests: Callable) -> str:
    """Write the task tree, and hand the seam-specific parts to `write_tests`."""
    facts = statement.Facts(
        name=spec.name, scale=spec.scale, description=spec.description,
        source_language=spec.language, target_language=spec.target_language,
        probes=corpus.probes, graded_points=corpus.graded_points,
        freeze_runs=corpus.runs,
        channels=("the value the call returned",), timed_workloads=len(corpus.timed),
        forbidden=tuple(spec.environment.get("forbidden", ())))

    package = harbor.Package(
        name=spec.name, scale=spec.scale, description=spec.description,
        instruction=statement.render(facts), source_language=spec.language,
        target_language=spec.target_language,
        provenance={"origin": spec.environment.get("origin") or spec.name,
                    # The same three numbers the statement quotes. Passed explicitly because the
                    # sentence in the shipped description is built from them, and defaulting them
                    # to zero produced a task whose provenance said "0 probes, 0 runs" beside an
                    # instruction that correctly said 57 and 5 -- the one claim a reader checks.
                    "probes": corpus.probes, "freeze_runs": facts.freeze_runs,
                    "adequacy": corpus.adequacy, "evidence": checks.to_json(),
                    "discard_rate": round(corpus.discard_rate, 4),
                    "capability": capability(spec.language, scale=spec.scale).__dict__})

    path = os.path.join(destination, spec.name)
    harbor.write(path, package)
    write_tests(path, corpus)
    # E9 READS WHAT WAS WRITTEN, so it cannot run with the rest of the battery: the file does not
    # exist until this function has. It lives here rather than in the pipeline because WHICH file
    # carries the schema is a fact about this layout, and a scale is free to emit another one.
    checks.record(evidence.harbor_schema_valid(os.path.join(path, "task.toml")))
    # E4 READS WHAT WAS WRITTEN -- same reason as E9. Checks both directories: the solver's tree
    # for answer-key artefacts (the original implementation is expected and allowed; the factory's
    # frozen expectations, verifier, or reference copy are not), and the verifier's directory for
    # whether the one setting that seals it is actually present in the emitted file.
    checks.record(evidence.no_runnable_reference(
        _reference_reachable_from(path),
        {"solver tree": os.path.join(path, "environment"), "verifier": path}))
    return path


def _reference_reachable_from(task_root: str):
    """Bind E4's per-directory probe to this layout. -> the callable E4 takes."""
    def find(directory: str) -> list[str]:
        if directory == task_root:
            return harbor.verifier_directory_is_unreachable(task_root)
        return harbor.runnable_reference_in(directory)
    return find


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
