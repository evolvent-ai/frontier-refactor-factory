"""One candidate, all eight stages, a real subprocess, a task on disk.

This is the test that says the parts fit. Everything else checks a piece in isolation; this drives
the whole pipeline against a subject that really runs, and asserts on the artefact rather than on
the machinery that produced it.

The subject is a genuine Python program started over the wire, not a stub. A mocked subject would
prove that the mock behaves as written, which is never the thing in doubt.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf import Factory                                                # noqa: E402
from frf.core import adequacy, pipeline                                # noqa: E402
from frf.core.scale import Candidate, Spec                             # noqa: E402
from frf.observe.call import shims, stages                             # noqa: E402
from frf.observe.call.runner import Subject                            # noqa: E402

# A subject with a branch and a refusal, so that a corpus can be inadequate in a visible way and a
# mutation has something to change.
_SUBJECT = """
def entry(values, threshold):
    if threshold < 0:
        raise ValueError("threshold must not be negative")
    kept = [v for v in values if v >= threshold]
    return {"kept": kept, "total": sum(kept)}
"""

# The same program with one comparison flipped. Used only to check that the verifier notices.
_MUTANT = _SUBJECT.replace("v >= threshold", "v > threshold")


class _Probes:
    """A schema-sampled corpus, as the module scale would produce.

    Sized above the pipeline's floor on purpose. On this seam one probe is worth one point, so
    clearing MIN_GRADED_POINTS needs that many surviving probes -- three of which are then held out
    for timing and stop being graded.
    """

    count = 50

    def draw(self, count: int):
        return [[[i, i + 1, i + 2, i * 2], i % 4] for i in range(count)]


class _Observer:
    """The call seam, bound to a workspace on disk."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace

    def build(self, spec: Spec) -> None:
        shim = shims.load(spec.language)
        with open(os.path.join(self.workspace, shim.template), "w") as handle:
            handle.write(shims.source(shim))
        self._write_subject(_SUBJECT)
        _, self._argv = shim.commands(self.workspace)

    def _write_subject(self, body: str) -> None:
        with open(os.path.join(self.workspace, "subject.py"), "w") as handle:
            handle.write(body)

    def subject(self, spec: Spec, *, mutated: bool = False) -> Subject:
        self._write_subject(_MUTANT if mutated else _SUBJECT)
        return Subject(self._argv, cwd=self.workspace)

    def coverage(self):
        return adequacy.NullCoverage()

    def forbidden_references(self, spec: Spec) -> list:
        return []

    def isolated(self) -> bool:
        return True


class _ModuleScale:
    """The four answers, and nothing else."""

    name = "module"

    def __init__(self, observer: _Observer) -> None:
        self._observer = observer

    def find(self, budget: int):
        return [Candidate("test://subject/%d" % i, "module", "python", "fixture")
                for i in range(budget)]

    def specify(self, candidate: Candidate) -> Spec:
        return Spec(name="threshold-filter", scale="module", language="python",
                    description="Keeps the values at or above a threshold and sums them. "
                                "A negative threshold is refused.",
                    invoke=["python3", "serve.py"], entry="entry")

    def observe(self):
        return self._observer

    def probes(self, spec: Spec):
        return _Probes()


def _factory(workspace: str, destination: str) -> Factory:
    observer = _Observer(workspace)

    def write_tests(path: str, corpus) -> None:
        """The seam-specific half of emission: expectations and inputs, digests only."""
        with open(os.path.join(path, "tests", "expectations.jsonl"), "w") as handle:
            for expectation in corpus.expectations:
                handle.write(json.dumps(expectation.to_json()) + "\n")
        with open(os.path.join(path, "tests", "probes.json"), "w") as handle:
            json.dump(corpus.inputs, handle)

    def drive(path: str) -> tuple[int, int]:
        """Replay the emitted package with the reference, scoring what the package itself holds."""
        from frf.observe.call import observation as obs

        expectations = [obs.Expectation.from_json(json.loads(line))
                        for line in open(os.path.join(path, "tests", "expectations.jsonl"))]
        inputs = json.load(open(os.path.join(path, "tests", "probes.json")))
        passed = total = 0
        with observer.subject(None) as subject:
            for expectation in expectations:
                got, want, _ = obs.grade(expectation, subject.call("run", inputs[expectation.probe_id]))
                passed += got
                total += want
        return passed, total

    # The seam is handed the SCALE, not the observer: the pipeline builds through the seam and
    # freezes through `scale.observe()`, so those two must reach the same object.
    scale = _ModuleScale(observer)
    seam = stages.Seam(scale, destination=destination, write_tests=write_tests, drive=drive)
    return Factory().register(scale).install_stages(**seam.stages())


def test_one_candidate_becomes_a_task_on_disk():
    """The whole pipeline, against a subject that really runs."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        result = _factory(workspace, out).build("module", budget=1)

        assert len(result) == 1, result.summary()
        task = result.tasks[0]

        # The floors are real: the corpus has to be worth grading.
        assert task.probes >= pipeline.MIN_PROBES
        assert task.graded_points >= pipeline.MIN_GRADED_POINTS
        assert task.discard_rate == 0.0, "a deterministic subject loses no probes"

        # And the artefact is a Harbor task, not a directory of intermediates.
        for relative in ("task.toml", "instruction.md", "tests/test.sh",
                         "tests/expectations.jsonl", "tests/probes.json"):
            assert os.path.exists(os.path.join(task.path, relative)), relative


def test_the_shipped_expectations_hold_no_answers():
    """Digests only. An expectation that stores the value is one filesystem mistake from being a key
    a submission can read and replay."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("module", budget=1).tasks[0]

        body = open(os.path.join(task.path, "tests", "expectations.jsonl")).read()
        assert "sha256:" in body
        assert '"kept"' not in body, "the subject's actual output must not appear"
        assert '"total"' not in body


def test_the_statement_states_the_rules_and_not_the_behaviour():
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("module", budget=1).tasks[0]
        text = open(os.path.join(task.path, "instruction.md")).read()

        assert "score = 0.5" in text and "Do not call" in text
        assert "graded observation(s)" in text
        # The description is present; the enumerated behaviour list is not.
        assert "Keeps the values at or above a threshold" in text
        assert "threshold must not be negative" not in text, "the refusal message is an answer"


def test_every_evidence_check_ran_and_the_battery_travels_with_the_task():
    """A task ships with the evidence for it, so a reader months later can see what was checked."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("module", budget=1).tasks[0]

        checks = {v["check"] for v in task.battery}
        assert {"ceiling", "floor", "channels-bite", "points-are-about-the-subject",
                "cannot-delegate-to-the-reference", "package-reproduces-itself"} <= checks

        by_name = {v["check"]: v for v in task.battery}
        assert by_name["points-are-about-the-subject"]["outcome"] == "not-applicable"
        assert "return value" in by_name["points-are-about-the-subject"]["detail"]


def test_the_emitted_task_toml_is_what_a_harness_reads():
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("module", budget=1).tasks[0]
        toml = open(os.path.join(task.path, "task.toml")).read()

        assert 'name = "module/threshold-filter"' in toml
        assert "gpus = 0" in toml, "the GPU fields are always written, so a GPU task changes nothing"
        assert 'environment_mode = "separate"' in toml, "the verifier's directory is not the solver's"


def test_a_subject_that_will_not_repeat_itself_is_refused_as_material():
    """A nondeterministic subject is the material's problem, and the refusal has to say so -- filing
    it as our fault would send a repair loop to fix a factory that is working."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        factory = _factory(workspace, out)
        observer = factory._scales["module"]._observer

        original = observer.subject

        def flaky(spec, *, mutated: bool = False):
            observer._write_subject(
                "import random\n"
                "def entry(values, threshold):\n"
                "    return random.random()\n")
            return Subject(observer._argv, cwd=observer.workspace)

        observer.subject = flaky
        try:
            result = factory.build("module", budget=1)
        finally:
            observer.subject = original

        assert len(result) == 0
        refusal = result.batch.refused[0]
        assert refusal.fault is pipeline.Fault.MATERIAL
        assert refusal.reason == "will-not-repeat-itself"


def test_the_verifier_notices_a_mutation_that_provably_changed_the_answer():
    """E3, end to end. A mutant scoring full marks is ambiguous -- blind verifier, or a mutation
    that never reached anything graded -- so the check requires the observation to have MOVED."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("module", budget=1).tasks[0]

        bite = next(v for v in task.battery if v["check"] == "channels-bite")
        assert bite["outcome"] == "holds", bite

def test_the_task_directory_is_self_contained_enough_to_inspect():
    """A person should be able to read what shipped without the factory that made it."""
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as out:
        task = _factory(workspace, out).build("module", budget=1).tasks[0]
        listing = subprocess.run(["find", task.path, "-type", "f"],
                                 capture_output=True, text=True).stdout
        assert "task.toml" in listing and "instruction.md" in listing
