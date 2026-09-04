"""The shared stages, as the process seam implements them.

The mirror of the call seam's file. Same six stages, same interfaces, same pipeline driving them --
and three differences, each of which is the reason the two seams could not be one implementation.

    A STEP IS WORTH FOUR POINTS, not one. Exit code, stdout, stderr and the resulting directory are
    graded separately, so a scenario of six steps is worth twenty-four observations rather than six.

    INSTABILITY IS MASKED BY POSITION, not discarded. A clock on line 7 costs line 7 and nothing
    else, because a line number is a coordinate that survives between runs.

    E5 APPLIES HERE. A step can be pure shell that never invokes the program, and its four channels
    then record what the host's shell did. On the call seam that cannot be constructed; here it has
    happened, so the check runs.

Everything else -- five runs, the floors, the battery's shape, the emitted layout -- is identical,
and identical because it is the same code in `core` reached through the same interfaces.
"""
from __future__ import annotations

import collections
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from ...core import adequacy, evidence, harbor, statement
from ...core.capabilities import capability
from ...core.scale import Spec
from . import observation as obs

# What a trivial submission looks like on this seam. Three of them, because the floor is the WORST
# outcome and trying one is trying your luck: on a real task `exit 0` scored 50% where `exit 1`
# scored 75%, and whichever a solver stumbles into first decides what the task is worth.
TRIVIAL = {
    "exit-0": "#!/bin/sh\nexit 0\n",
    "exit-1": "#!/bin/sh\nexit 1\n",
    "print-and-exit-0": '#!/bin/sh\nprintf "done\\n"\nexit 0\n',
}


# What a shell and this factory's own verifier both report when the program is not there. A corpus
# made entirely of these measured nothing; see the refusal in `freeze`.
PROGRAM_NOT_FOUND = 127


@dataclass
class Corpus:
    """What a freeze produced. Same four attributes the pipeline reads on either seam."""

    scenarios: list = field(default_factory=list)
    expectations: dict = field(default_factory=dict)     # probe_id -> [Expectation per step]
    discard_rate: float = 0.0
    usable: bool = True
    # WHY IT IS NOT USABLE, in the seam's own words. The pipeline prints this instead of its own
    # generic "the reference did not reproduce its probes", which is a specific diagnosis and the
    # wrong one for most of the ways a freeze comes back empty.
    unusable_reason: str = ""
    adequacy_note: str = ""
    adequacy: dict = field(default_factory=dict)
    timed: list = field(default_factory=list)
    # HOW MANY RUNS THIS WAS ACTUALLY DISTILLED FROM, not how many were requested. The two differ
    # whenever a run is cut short, and a statement quoting the configured number would be claiming
    # evidence nobody collected.
    runs: int = 0

    @property
    def probes(self) -> int:
        return len(self.expectations)

    @property
    def graded_points(self) -> int:
        return sum(step.graded_points()
                   for steps in self.expectations.values() for step in steps)



def freeze(spec: Spec, observer, source, *, runs: int) -> Corpus:
    """Run every scenario `runs` times and keep, per channel, only what repeated exactly.

    A scenario whose EVERY channel of EVERY step came out ungradeable is dropped and counted; one
    that lost a line to a clock is kept, because losing that line is what masking is for.
    """
    scenarios = list(source.draw(source.count))
    try:
        max_seconds = float(os.environ.get("FRF_FREEZE_MAX_SECONDS", "180"))
    except ValueError:
        max_seconds = 180.0
    deadline = time.monotonic() + max_seconds
    frozen, dropped = {}, 0
    # Whether the reference was ever actually executed, and whether it ever did any WORK.
    # See the two refusals below; they are the same question asked one level apart.
    ever_ran = False
    # WHICH scenarios did work, not merely WHETHER any did. A boolean was credited before the
    # scenario had been accepted into the corpus, so one that did work and was then discarded for
    # being irreproducible still opened the gate -- and what remained to be graded could be error
    # paths exclusively. Measured: four tasks emitted after the gate existed had empty stdout and a
    # non-zero exit on every graded step. The same set also answers the timing holdout, which takes
    # scenarios OUT of grading after this loop has finished.
    worked: set = set()
    why: dict = collections.Counter()

    batched = getattr(observer, "run_many", None)
    all_runs = ([batched(spec, scenarios) for _ in range(runs)] if batched is not None else None)
    for scenario in scenarios:
        if time.monotonic() >= deadline:
            return Corpus(scenarios=[], expectations={}, discard_rate=1.0,
                          usable=False, timed=[], runs=runs)
        if batched is None:
            runs_observed = [observer.run(spec, scenario) for _ in range(runs)]
        else:
            runs_observed = [batch.get(scenario.probe_id, []) for batch in all_runs]
        # A SCENARIO WHOSE EVERY RUN SAID "NOT FOUND" MEASURED NOTHING, so it is dropped here
        # rather than frozen. Freezing it would succeed -- 127 repeats perfectly -- and produce an
        # expectation that grades a submission on reproducing a missing program.
        if runs_observed and all(
                int(getattr(one, "exit_code", 0) or 0) == PROGRAM_NOT_FOUND
                for observed in runs_observed for one in observed):
            dropped += 1
            continue
        ever_ran = True
        # DID IT DO ANYTHING? A program invoked wrongly prints its usage to stderr, writes nothing
        # to stdout, changes no file and exits non-zero -- every time, identically. See the refusal
        # below for what that costs.
        steps = [obs.freeze(index, [observed[index] for observed in runs_observed])
                 for index in range(len(scenario.steps))]
        if any(step.graded_points() for step in steps):
            frozen[scenario.probe_id] = steps
            # ASKED OF THE FROZEN CONSENSUS, AND ONLY ONCE THE SCENARIO IS ACTUALLY KEPT. This used to
            # read `any(did_work(one) for observed in runs_observed for one in observed)` above the
            # freeze, which was wrong twice over:
            #
            #   - it ORed across all five runs, so one lucky success among five failures opened the
            #     gate while the frozen consensus -- the thing that ships -- recorded the failure;
            #   - it credited the scenario before line 129 decided whether to keep it, so a scenario
            #     that did work and was then dropped for having no gradable channel still vouched for
            #     a corpus it is absent from.
            #
            # MEASURED: `repo/hucre-faster` emitted with 79 graded steps and not one doing work, with
            # no installer error to explain it -- this gate was credited by runs whose own frozen
            # expectations disagree. Judging what ships is the only question worth asking, because that
            # is what a submission is graded against.
            if obs.froze_work(steps):
                worked.add(scenario.probe_id)
        else:
            dropped += 1
            # WHY IT WAS DROPPED, not only that it was. `100% of probes were discarded` names the
            # outcome and leaves the cause -- a clock in stderr, a line count that moves, a tree
            # that gains a temp file -- to be guessed at. The freeze already computed the reason
            # per channel and threw it away.
            for step in steps:
                for name in ("exit_code", "stdout", "stderr", "tree"):
                    channel = step.channel(name)
                    if not channel.graded and channel.reason:
                        why["%s: %s" % (name, channel.reason)] += 1

    # DID THE REFERENCE ACTUALLY RUN? A program that was never found exits 127 on every scenario,
    # and that is PERFECTLY REPRODUCIBLE: five runs agree exactly, every channel freezes, `ceiling`
    # scores the reference 100% against its own frozen failure, and E7 replays it happily. Every
    # ruler this factory has passes, because each of them asks whether the measurement can be
    # trusted and none of them asks whether anything was measured at all.
    #
    # Not hypothetical: 14 of 25 attested repo tasks in one corpus exited 127 on EVERY scenario.
    # They graded a submission on reproducing "command not found". The cause is the same one behind
    # the rest of this file's history -- the reference is installed by the Dockerfile the task
    # SHIPS, and the freeze runs in the sandbox that PRODUCES it, where nothing installed it.
    #
    # `not ever_ran`, rather than refusing any 127: a scenario that exercises a missing-argument or
    # bad-input path may exit 127 by itself, and that is real material worth keeping.
    if scenarios and not ever_ran:
        return Corpus(scenarios=[], expectations={}, discard_rate=1.0, usable=False,
                      adequacy_note="the reference exited %d on every scenario -- it was never "
                                    "found, so nothing was measured" % PROGRAM_NOT_FOUND,
                      timed=[], runs=runs)

    # ...AND DID IT DO ANY WORK? The check above catches a program that was never found. This
    # catches one that was found, ran, and did nothing: invoked without the arguments it needs, it
    # prints usage to stderr, writes nothing to stdout, touches no file and exits 2. That is as
    # reproducible as exit 127 and passes exactly the same rulers -- five runs agree, every channel
    # freezes, `ceiling` scores the reference 100% against its own frozen failure.
    #
    # 76 of 82 repo tasks in one corpus had EMPTY STDOUT on every graded scenario -- 93% of
    # everything the scale had ever produced, grading a submission on reproducing an error message.
    #
    # The rule is corpus-level, not per-scenario, on purpose: a program's error path is real
    # material when the corpus also contains it working. What is not material is a corpus made
    # entirely of error paths.
    # ASKED OF THE SCENARIOS THAT SURVIVED, not of every scenario that was tried. A scenario that
    # did work and was then dropped for being irreproducible says nothing about what is left to
    # grade, and crediting it here is how four tasks shipped whose every graded step had empty
    # stdout and a non-zero exit.
    if scenarios and ever_ran and not (worked & set(frozen)):
        return Corpus(scenarios=[], expectations={}, discard_rate=1.0, usable=False,
                      adequacy_note="the reference wrote nothing to stdout and exited non-zero on "
                                    "every scenario that froze -- it was invoked but never did any "
                                    "work",
                      timed=[], runs=runs)

    attempted = len(scenarios)
    rate = (dropped / attempted) if attempted else 0.0
    timed = _pick_timed(list(frozen), worked=worked)
    graded = {pid: steps for pid, steps in frozen.items() if pid not in set(timed)}

    # AND THE HOLDOUT MUST NOT HAVE TAKEN THE ONLY WORKING SCENARIO. `_pick_timed` keeps one back,
    # but it can only do so from what it was given; if every working scenario is also the only
    # material for timing there is nothing to grade that shows the program working.
    if graded and not (worked & set(graded)):
        return Corpus(scenarios=[], expectations={}, discard_rate=1.0, usable=False,
                      adequacy_note="every scenario that did real work was held out for timing, so "
                                    "the graded corpus contains only error paths",
                      timed=[], runs=runs)

    usable = bool(graded) and rate <= 0.25
    reason = ""
    if not usable and why:
        reason = ("%.0f%% of scenarios were discarded; what they disagreed on -- %s"
                  % (100 * rate, ", ".join("%s (x%d)" % (text, count)
                                           for text, count in why.most_common(3))))
    return Corpus(scenarios=[s for s in scenarios if s.probe_id in frozen],
                  expectations=graded, discard_rate=rate, unusable_reason=reason,
                  usable=usable, timed=timed, runs=runs)


def _pick_timed(probe_ids: list, count: int = 3, *, worked: set | None = None) -> list:
    """Which scenarios become the timing workload, held out of grading.

    Held out because correctness runs first: a scenario that is both graded and timed can be
    answered honestly, cached by argv, and replayed when the clock starts -- full marks and an
    arbitrary speedup with nothing implemented.

    `worked` NAMES THE SCENARIOS WORTH TIMING, and taking the tail blindly got that backwards twice
    over. The tail is where the stdin cases land, which are the ones a program run without arguments
    rejects -- so the holdout was made of error paths, and timing measured how fast the reference
    prints its usage. Meanwhile the scenarios that did real work stayed in the graded set only by
    luck; when the luck ran the other way the graded set was error paths exclusively.

    So the holdout is drawn from working scenarios, and at least one is always left behind to be
    graded: a corpus needs both halves to show the program working.
    """
    if not worked:
        return probe_ids[-count:] if len(probe_ids) > count else []
    working = [pid for pid in probe_ids if pid in worked]
    # One stays behind, whatever else happens -- see the second gate in `freeze`.
    available = max(0, len(working) - 1)
    if available <= 0:
        return []
    return working[-min(count, available):]


def audit(spec: Spec, observer, corpus: Corpus) -> Corpus:
    """Reach and floor, as on the other seam. The verdict lives in `core.adequacy`.

    This now calls the repair loop (DESIGN §7), which will attempt to improve coverage by proposing
    new scenarios for dark regions.
    """
    def _refreeze(corpus, probe_id, scenario_dict):
        """Observe one new scenario through the reference and freeze the results into the corpus."""
        step = {
            "argv": scenario_dict.get("cmd", ["{PROGRAM}"]),
            "cwd": scenario_dict.get("cwd", "."),
            "stdin": scenario_dict.get("stdin") or None,
        }
        scenario_data = {
            "probe_id": probe_id,
            "steps": [step],
            "fixture": scenario_dict.get("fixture"),
            "environment": scenario_dict.get("env") or {},
        }
        from .runner import Scenario
        scenario = Scenario.from_json(scenario_data)
        observations = observer.run(spec, scenario)
        steps = [
            obs.freeze(step_idx, [observations[step_idx]])
            for step_idx in range(len(scenario.steps))
            if step_idx < len(observations)
        ]
        if any(step.graded_points() for step in steps):
            corpus.scenarios.append(scenario)
            corpus.expectations[probe_id] = steps

    return adequacy.repair(
        spec, observer, corpus,
        score_trivial=lambda name: _score_trivial(observer, spec, corpus, name),
        trivial_names=list(TRIVIAL),
        max_iterations=3,
        log=lambda msg: None,
        refreeze=_refreeze,
    )


def _score(corpus: Corpus, observed_by_probe: dict) -> tuple[int, int]:
    passed = total = 0
    for probe_id, steps in corpus.expectations.items():
        observed = observed_by_probe.get(probe_id) or []
        for index, expectation in enumerate(steps):
            actual = observed[index] if index < len(observed) else obs.Observation(127)
            got, want, _ = obs.grade(expectation, actual)
            passed += got
            total += want
    return passed, total


def _score_trivial(observer, spec: Spec, corpus: Corpus, kind: str) -> tuple[int, int]:
    return _score(corpus, observer.run_all(spec, corpus.scenarios, submission=TRIVIAL[kind]))


def _score_reference(observer, spec: Spec, corpus: Corpus) -> tuple[int, int]:
    return _score(corpus, observer.run_all(spec, corpus.scenarios))


def battery(spec: Spec, observer, corpus: Corpus) -> evidence.Battery:
    """The eight checks. E5 applies on this seam and is measured rather than skipped."""
    checks = evidence.Battery()
    checks.record(evidence.ceiling(lambda: _score_reference(observer, spec, corpus)))
    checks.record(evidence.floor(
        lambda name: _score_trivial(observer, spec, corpus, name), list(TRIVIAL)))
    checks.record(evidence.channels_bite(
        lambda channel: _perturb(observer, spec, corpus, channel), list(obs.CHANNELS)))
    checks.record(evidence.points_are_about_the_subject(
        lambda: _steps_touching_subject(corpus), applies=True))
    checks.record(evidence.cannot_delegate_to_the_reference(
        lambda: observer.forbidden_references(spec), lambda: observer.isolated()))
    return checks


def _steps_touching_subject(corpus: Corpus) -> tuple[int, int]:
    """E5: how many scored steps actually invoke the program under test.

    A scenario harvested from a shell script can contain steps that only stage fixtures. Those steps
    still produce four channels, and grading them grades the host's shell -- a task built mostly
    from them measures /bin/sh while passing every other check in the battery.
    """
    touching = total = 0
    by_id = {s.probe_id: s for s in corpus.scenarios}
    for probe_id, steps in corpus.expectations.items():
        scenario = by_id.get(probe_id)
        for index, expectation in enumerate(steps):
            if not expectation.graded_points():
                continue
            total += 1
            step = scenario.steps[index] if scenario and index < len(scenario.steps) else None
            if step and any("{PROGRAM}" in str(part) for part in step.argv):
                touching += 1
    return touching, total


def _perturb(observer, spec: Spec, corpus: Corpus, channel: str) -> tuple[bool, bool]:
    """-> (the channel provably moved, the verifier noticed).

    Both halves. A mutant scoring full marks is ambiguous -- blind verifier, or a mutation that never
    reached anything graded -- and a check that reports only the score cannot tell them apart.
    """
    observed = observer.run_all(spec, corpus.scenarios, mutated=channel)
    diverged = caught = False
    for probe_id, steps in corpus.expectations.items():
        actuals = observed.get(probe_id) or []
        for index, expectation in enumerate(steps):
            expected = expectation.channel(channel)
            if not expected.graded or index >= len(actuals):
                continue
            actual = actuals[index]
            if channel == "exit_code":
                moved = obs._digest(str(actual.exit_code)) != expected.digest
            else:
                stream = actual.channel(channel)
                moved = (len(stream.lines) != expected.line_count
                         or stream.digest(expected.masked) != expected.digest)
            diverged = diverged or moved
            got, want, _ = obs.grade(expectation, actual)
            caught = caught or got != want
    return diverged, caught


def emit(destination: str, spec: Spec, corpus: Corpus, checks: evidence.Battery,
         *, write_tests: Callable) -> str:
    facts = statement.Facts(
        name=spec.name, scale=spec.scale, description=spec.description,
        source_language=spec.language, target_language=spec.target_language,
        probes=corpus.probes, graded_points=corpus.graded_points,
        freeze_runs=corpus.runs,
        channels=("exit code", "stdout", "stderr", "the resulting directory"),
        timed_workloads=len(corpus.timed),
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
    # E4 READS WHAT WAS WRITTEN, for the same reason E9 does: both directories it asks about exist
    # only once this function has run. Both halves are checked -- the solver's tree for a copy of
    # the answer key, and the verifier's own directory for whether one setting still seals it.
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


class Seam:
    """The six shared stages, bound to the SCALE. Handed to `Factory.install_stages`.

    The scale rather than one observer, for the reason set out at length in the call seam's
    equivalent: the pipeline builds through the seam and freezes through `scale.observe()`, so
    capturing an observer here makes those two different objects -- and across a batch it would
    serve the first candidate's program for every task after the first.
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
            "replay": lambda path: self._drive(path),
        }
