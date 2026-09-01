"""The process seam, driven through the whole pipeline against a real program.

The subject is a shell script that reads a file and writes another. Small, but a genuine process: it
has an exit code, two streams and a directory it changes, which is all four channels and the only
thing this seam claims to observe.

The test that matters most is the last one. E5 does not apply on the call seam because no observation
can exist without the subject having run; here it can, and this shows the check catching it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf import Factory                                                # noqa: E402
from frf.core import adequacy, pipeline                                # noqa: E402
from frf.core.scale import Candidate, Spec                             # noqa: E402
from frf.observe.process import stages                                 # noqa: E402
from frf.observe.process.runner import Scenario, Step, run_scenario    # noqa: E402

# A program with a branch, a refusal, and a file it writes. Everything a four-channel observation
# needs in order not to be constant.
_PROGRAM = """#!/bin/sh
if [ ! -f "$1" ]; then
    echo "no such input: $1" >&2
    exit 2
fi
wc -l < "$1" | tr -d ' ' > count.txt
echo "counted $(cat count.txt) line(s)"
"""

# The same program that miscounts. Used only to check the verifier notices.
_MUTANT = _PROGRAM.replace('wc -l < "$1"', 'wc -w < "$1"')


class _Scenarios:
    count = 14

    def __init__(self, fixtures: str) -> None:
        self.fixtures = fixtures

    def draw(self, count: int):
        made = []
        for i in range(count):
            # The input is staged by the FIXTURE, not by a graded step. A setup step would be
            # scored on four channels that record what the shell did, which is precisely the defect
            # E5 exists to catch -- and it caught this fixture on the first run.
            body = "".join("line %d\n" % n for n in range(i + 1))
            made.append(Scenario("probe-%02d" % i, [
                Step(["{PROGRAM}", "input.txt"]),
                Step(["{PROGRAM}", "missing.txt"]),
            ], fixture="probe-%02d.tar" % i))
            _make_fixture(self.fixtures, "probe-%02d" % i, body)
        return made


def _make_fixture(fixtures: str, name: str, body: str) -> None:
    """One scenario's starting directory, as a tarball. Staged before step 0 rather than by a step,
    so that no graded observation belongs to the shell that prepared it."""
    import tarfile

    os.makedirs(fixtures, exist_ok=True)
    staging = os.path.join(fixtures, name)
    os.makedirs(staging, exist_ok=True)
    with open(os.path.join(staging, "input.txt"), "w") as handle:
        handle.write(body)
    with tarfile.open(os.path.join(fixtures, "%s.tar" % name), "w") as archive:
        archive.add(os.path.join(staging, "input.txt"), arcname="input.txt")


class _Observer:
    """The process seam bound to a workspace, with a reference and a mutant on disk."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.reference = os.path.join(workspace, "reference.sh")
        self.fixtures = os.path.join(workspace, "fixtures")

    def build(self, spec: Spec) -> None:
        self._write(self.reference, _PROGRAM)

    @staticmethod
    def _write(path: str, body: str) -> None:
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)

    def run(self, spec: Spec, scenario: Scenario) -> list:
        return run_scenario(scenario, [self.reference], fixtures_dir=self.fixtures)

    def run_all(self, spec: Spec, scenarios: list, *, submission: str | None = None,
                mutated: str | None = None) -> dict:
        program = self.reference
        if submission is not None or mutated is not None:
            program = os.path.join(self.workspace, "candidate.sh")
            self._write(program, submission if submission is not None else _MUTANT)
        return {s.probe_id: run_scenario(s, [program], fixtures_dir=self.fixtures)
                for s in scenarios}

    def coverage(self):
        return adequacy.NullCoverage()

    def forbidden_references(self, spec: Spec) -> list:
        return []

    def isolated(self) -> bool:
        return True


class _RepoScale:
    name = "repo"

    def __init__(self, observer: _Observer) -> None:
        self._observer = observer

    def find(self, budget: int):
        return [Candidate("test://prog/%d" % i, "repo", "sh", "fixture") for i in range(budget)]

    def specify(self, candidate: Candidate) -> Spec:
        return Spec(name="line-counter", scale="repo", language="sh",
                    description="Counts the lines of a file, writes the count beside it, and "
                                "refuses a path that does not exist.",
                    invoke=["./reference.sh"])

    def observe(self):
        return self._observer

    def probes(self, spec: Spec):
        return _Scenarios(self._observer.fixtures)


def _factory(workspace: str, destination: str) -> Factory:
    observer = _Observer(workspace)

    def write_tests(path: str, corpus) -> None:
        with open(os.path.join(path, "tests", "expectations.json"), "w") as handle:
            json.dump({pid: [step.to_json() for step in steps]
                       for pid, steps in corpus.expectations.items()}, handle)
        with open(os.path.join(path, "tests", "scenarios.jsonl"), "w") as handle:
            for scenario in corpus.scenarios:
                handle.write(json.dumps(scenario.to_json()) + "\n")

    def drive(path: str) -> tuple[int, int]:
        from frf.observe.process import observation as obs

        loaded = json.load(open(os.path.join(path, "tests", "expectations.json")))
        scenarios = {s["probe_id"]: Scenario.from_json(s)
                     for s in (json.loads(line) for line
                               in open(os.path.join(path, "tests", "scenarios.jsonl")))}
        passed = total = 0
        for probe_id, steps in loaded.items():
            observed = run_scenario(scenarios[probe_id], [observer.reference],
                                    fixtures_dir=observer.fixtures)
            for index, raw in enumerate(steps):
                expectation = obs.Expectation.from_json(raw)
                actual = observed[index] if index < len(observed) else obs.Observation(127)
                got, want, _ = obs.grade(expectation, actual)
                passed += got
                total += want
        return passed, total

    # The seam is handed the SCALE, not the observer -- see the call seam's equivalent.
    scale = _RepoScale(observer)
    seam = stages.Seam(scale, destination=destination, write_tests=write_tests, drive=drive)
    return Factory().register(scale).install_stages(**seam.stages())


def test_a_process_subject_becomes_a_task():
    """Four channels per step, so a modest corpus is worth many points."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        result = _factory(workspace, out).build("repo", budget=1)

        assert len(result) == 1, result.summary()
        task = result.tasks[0]
        assert task.graded_points >= pipeline.MIN_GRADED_POINTS
        # Three steps, up to four channels each: a scenario is worth far more than one point, which
        # is the difference from the call seam.
        assert task.graded_points > task.probes


def test_the_statement_counts_four_channels():
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("repo", budget=1).tasks[0]
        text = open(os.path.join(task.path, "instruction.md")).read()

        for channel in ("exit code", "stdout", "stderr", "the resulting directory"):
            assert channel in text, channel


def test_e5_applies_here_and_is_measured():
    """The check that is NOT_APPLICABLE on the call seam runs on this one.

    Two of every three steps in this corpus invoke the program; the first only stages a fixture. So
    the check must report a real ratio rather than a skip -- and must not fail, since most points do
    concern the subject.
    """
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("repo", budget=1).tasks[0]

        check = next(v for v in task.battery if v["check"] == "points-are-about-the-subject")
        assert check["outcome"] != "not-applicable", "this seam can construct the failure"
        assert "scored step" in check["detail"]


def test_every_channel_is_shown_to_bite():
    """A channel that is graded but constant is a channel that grades nothing."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("repo", budget=1).tasks[0]

        bite = next(v for v in task.battery if v["check"] == "channels-bite")
        assert bite["outcome"] == "holds", bite


def test_a_shell_only_corpus_is_caught_by_e5():
    """The defect E5 exists for: points that grade the host's shell rather than the program.

    Every other check in the battery passes on such a task -- the reference reproduces it, a blank
    submission scores zero -- which is exactly why this one is not optional.
    """
    class _ShellOnly:
        count = 14

        def draw(self, count: int):
            return [Scenario("probe-%02d" % i,
                             [Step(["sh", "-c", "echo %d > out.txt; echo done" % i])])
                    for i in range(count)]

    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        factory = _factory(workspace, out)
        factory._scales["repo"].probes = lambda spec: _ShellOnly()

        result = factory.build("repo", budget=1)
        assert len(result) == 0, "a corpus that never runs the program must not ship"

        refusal = result.batch.refused[0]
        # TWO GATES CATCH THIS, and which one speaks first is not the point. A corpus of pure
        # shell never runs the program, so `adequacy` sees a subject it reached none of, and E5
        # sees scoring points that are not about the subject. Both are correct findings about the
        # same defect; asserting on one stage name made the test fail the day the other started
        # firing, which is a test tracking an implementation detail rather than the property.
        assert refusal.stage in ("adequacy", "evidence"), refusal.stage
        detail = refusal.detail.lower()
        assert ("never invoke the subject" in detail      # E5's wording
                or "reach" in detail                       # adequacy, reach half
                or "does nothing already scores" in detail  # adequacy, floor half
                ), refusal.detail


def test_a_reference_that_never_ran_is_not_a_corpus():
    """A program that was never found exits 127 on every scenario -- perfectly reproducibly.

    Five runs agree exactly, every channel freezes, `ceiling` scores the reference 100% against its
    own frozen failure, and E7 replays it happily. Every ruler passes, because each asks whether the
    measurement can be trusted and none asks whether anything was measured. Fourteen of twenty-five
    attested repo tasks in one corpus graded a submission on reproducing "command not found".
    """
    from frf.observe.process import stages as process_stages

    class _Observation:
        def __init__(self, code):
            self.exit_code = code
            self.stdout = ""
            self.stderr = "could not execute"
            self.tree = ""

        def freeze(self, index, observed):
            raise AssertionError("a corpus of 127s must be refused before anything is frozen")

    class _Scenario:
        def __init__(self, pid):
            self.probe_id = pid
            self.steps = [object()]

    class _Source:
        count = 3

        def draw(self, _n):
            return [_Scenario("scenario-%04d" % i) for i in range(3)]

    class _Observer:
        def run(self, _spec, _scenario):
            return [_Observation(127)]

    corpus = process_stages.freeze(object(), _Observer(), _Source(), runs=2)

    assert not corpus.usable, "a corpus whose reference never ran must not be usable"
    assert corpus.expectations == {}
    assert "never" in corpus.adequacy_note and "127" in corpus.adequacy_note


def test_one_scenario_exiting_127_is_still_real_material(monkeypatch):
    """A scenario that exercises a missing-argument path may exit 127 by itself.

    Refusing on any 127 would throw away real material; the rule is that NOTHING ran, not that
    something returned 127.
    """
    from frf.observe.process import stages as process_stages

    class _Step:
        def graded_points(self):
            return 4

    monkeypatch.setattr(process_stages.obs, "freeze", lambda index, observed: _Step())

    class _Observation:
        def __init__(self, code):
            self.exit_code = code

    class _Scenario:
        def __init__(self, pid, code):
            self.probe_id = pid
            self.steps = [object()]
            self.code = code

    class _Source:
        count = 8

        def draw(self, _n):
            # One in eight, so the existing discard-rate rule (a corpus losing more than a quarter
            # of its scenarios is not usable) is not what this test is measuring.
            return ([_Scenario("scenario-0000", 127)]
                    + [_Scenario("scenario-%04d" % i, 0) for i in range(1, 8)])

    class _Observer:
        def run(self, _spec, scenario):
            return [_Observation(scenario.code)]

    corpus = process_stages.freeze(object(), _Observer(), _Source(), runs=2)

    assert corpus.usable, "one 127 among real runs is material, not a broken corpus"
    assert "scenario-0001" in corpus.expectations, "the scenario that ran was kept"
    assert "scenario-0000" not in corpus.expectations, "the one that never ran was dropped"
