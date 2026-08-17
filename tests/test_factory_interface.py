"""The public surface, driven the way a user of the library drives it.

The important test here is the last one. Everything else checks that the interface behaves; that one
checks the claim the whole design rests on -- that a new scale can be added without editing anything
in `core/`. It is written as a scale defined entirely inside this file, because that is exactly what
someone extending the library would write.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf import Factory, Settings                                      # noqa: E402
from frf.core import evidence, pipeline                                # noqa: E402
from frf.core.scale import Candidate, Spec                             # noqa: E402


class _Corpus:
    """What a freeze produces, in the shape the pipeline's gates read."""

    def __init__(self, probes: int, points: int, discard_rate: float = 0.0) -> None:
        self.probes, self.graded_points = probes, points
        self.discard_rate, self.usable = discard_rate, discard_rate <= 0.25
        self.adequacy_note = "reaches the subject"


class _ToyScale:
    """A scale that answers the four questions and implements none of the eight stages."""

    name = "toy"

    def __init__(self, language: str = "python") -> None:
        self.language = language

    def find(self, budget: int):
        return [Candidate("toy://%d" % i, "module", self.language, "test-index")
                for i in range(budget)]

    def specify(self, candidate: Candidate) -> Spec:
        return Spec(name=candidate.identity.rsplit("/", 1)[-1], scale="module",
                    language=self.language, description="a toy subject",
                    invoke=["python3", "subject.py"], entry="entry")

    def observe(self):
        return object()

    def probes(self, spec):
        return object()


def _stages(*, corpus=None, battery_ok=True, replay=(40, 40)):
    """The six shared stages, as the smallest things that satisfy the pipeline."""
    corpus = corpus or _Corpus(8, 40)

    def fresh_battery(spec, observer, report):
        # A NEW battery per candidate. Sharing one would let verdicts accumulate across a batch, so
        # the second candidate would be judged partly on the first one's evidence.
        battery = evidence.Battery()
        battery.record(evidence.ceiling(lambda: (40, 40) if battery_ok else (30, 40)))
        return battery

    return {"build": lambda spec: None,
            "freeze": lambda spec, observer, source, runs: corpus,
            "adequacy": lambda spec, observer, report: report,
            "battery": fresh_battery,
            "emit": lambda spec, report, battery: "/tmp/tasks/%s" % spec.name,
            "replay": lambda path: replay}


def _factory(**overrides) -> Factory:
    return Factory().register(_ToyScale()).install_stages(**_stages(**overrides))


def test_the_smallest_useful_program_is_three_lines():
    """Construct, register, build. Anything longer than this is a surface problem."""
    result = _factory().build("toy", budget=3)

    assert len(result) == 3, result.summary()
    assert result, "a Result with tasks in it is truthy"
    assert result.summary()["yield_rate"] == 1.0
    assert all(task.scale == "module" for task in result.tasks)


def test_asking_for_a_scale_nobody_registered_says_what_is_registered():
    """The failure a first-time user hits, so it has to be the most helpful message in the library."""
    try:
        Factory().build("module", 1)
    except LookupError as exc:
        assert "module" in str(exc) and "register" in str(exc)
    else:
        raise AssertionError("an unregistered scale must raise")


def test_a_scale_cannot_be_replaced_by_accident():
    """Two implementations under one name makes provenance a lie about which one ran."""
    factory = Factory().register(_ToyScale())
    try:
        factory.register(_ToyScale())
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("silently replacing a scale must not be possible")


def test_scales_are_listed_in_the_canonical_order():
    """kernel, module, package, repo -- smallest first, and anything unknown after."""
    class _Named(_ToyScale):
        def __init__(self, name):
            super().__init__()
            self.name = name

    factory = Factory()
    for name in ("repo", "custom", "kernel", "module"):
        factory.register(_Named(name))
    assert factory.scales == ["kernel", "module", "repo", "custom"]


def test_specific_candidates_can_be_supplied_so_one_failure_is_reproducible():
    """"This repository fails" needs a way to say only that repository."""
    only = Candidate("toy://the-one-that-fails", "module", "python", "bug-report")
    result = _factory().build("toy", budget=5, candidates=[only])

    assert len(result) == 1
    assert result.tasks[0].name == "the-one-that-fails"


def test_a_thin_corpus_is_refused_and_blamed_on_the_material():
    """Below the floor a corpus cannot tell a real submission from a lucky one."""
    result = _factory(corpus=_Corpus(2, 8)).build("toy", budget=1)

    assert len(result) == 0
    refusal = result.batch.refused[0]
    assert (refusal.stage, refusal.fault) == ("freeze", pipeline.Fault.MATERIAL)
    assert "lucky" in refusal.detail


def test_a_subject_that_will_not_repeat_itself_is_refused_before_anything_else_runs():
    result = _factory(corpus=_Corpus(8, 40, discard_rate=0.8)).build("toy", budget=1)

    refusal = result.batch.refused[0]
    assert refusal.reason == "will-not-repeat-itself"
    assert "selected by luck" in refusal.detail


def test_a_package_that_cannot_reproduce_itself_is_OUR_fault_not_the_material_s():
    """The distinction that decides where a repair loop looks.

    Everything up to emission passed, so the material was fine; what failed is the package we wrote.
    Filing that under "this candidate was unsuitable" is how a factory's own bugs get counted as a
    property of the material and never get fixed.
    """
    result = _factory(replay=(38, 40)).build("toy", budget=1)

    refusal = result.batch.refused[0]
    assert refusal.fault is pipeline.Fault.FACTORY, refusal.to_json()
    assert refusal.stage == "emit"
    assert not result.batch.trustworthy, "a batch dominated by our own faults is not a yield"


def test_a_batch_says_whether_its_yield_means_anything():
    """A yield measures the material only if the factory was mostly not the problem."""
    honest = _factory().build("toy", budget=10)
    assert honest.summary()["trustworthy"]
    assert honest.summary()["refused_factory"] == 0

    broken = _factory(replay=(1, 40)).build("toy", budget=10)
    summary = broken.summary()
    assert summary["refused_factory"] == 10 and not summary["trustworthy"]
    assert summary["by_reason"], "the reasons are counted, not just totalled"


def test_settings_travel_whole_so_a_run_can_be_explained_later():
    settings = Settings(freeze_runs=7, output_dir="/tmp/out")
    assert Factory(settings).settings.to_json()["freeze_runs"] == 7
    assert Settings().sandboxed is True, "sandboxing is the default, not an opt-in"


def test_a_new_scale_needs_no_change_to_core():
    """THE claim of this design, tested rather than asserted.

    `_ToyScale` is defined in this test file. It answers four questions, implements none of the
    eight stages, and ships a task through the whole pipeline. If adding a scale ever required
    editing `core/`, this test would have to import something from there that does not exist yet --
    and it does not.
    """
    import ast

    core = os.path.join(ROOT, "frf", "core")
    for name in sorted(os.listdir(core)):
        if not name.endswith(".py"):
            continue
        source = open(os.path.join(core, name)).read()
        for node in ast.walk(ast.parse(source)):
            # No module in core/ may name a scale: that would be a branch on which family it is
            # serving, which is exactly the coupling this interface exists to prevent.
            if isinstance(node, ast.If) and "kernel" in ast.dump(node) and "module" in ast.dump(node):
                raise AssertionError("%s branches on the scale" % name)

    assert len(_factory().build("toy", budget=1)) == 1
