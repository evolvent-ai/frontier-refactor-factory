"""The four scales, each asked the four questions it exists to answer.

What is checked here is mostly what a scale REFUSES. Each one is a thin adapter between an index and
a shared pipeline, so the interesting behaviour is at its edges: material it should not accept, and
the difference between it and its neighbour.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf import Factory                                                # noqa: E402
from frf.core.scale import SCALES, Candidate                           # noqa: E402
from frf.scales import Kernel, Module, Package, Repo                   # noqa: E402

_NUMERIC = {"params": [{"kind": "float_array", "dtype": "float64", "size": "n"}]}
_SCALAR = {"params": [{"kind": "int", "low": 0, "high": 100}]}


def _candidate(scale: str, **detail) -> Candidate:
    return Candidate("test://%s" % scale, scale, "python", "fixture", detail)


def test_all_four_register_and_list_in_the_canonical_order():
    factory = Factory()
    for implementation in (Repo(), Kernel(), Package(), Module()):
        factory.register(implementation)
    assert factory.scales == list(SCALES)


def test_a_module_needs_enough_detail_to_call_the_subject():
    """An index that supplies half a candidate fails here, where the message can say which half --
    not inside a freeze, where it looks as though the subject misbehaved."""
    module = Module()
    try:
        module.specify(_candidate("module", symbol="f"))
    except ValueError as exc:
        assert "source_path" in str(exc) and "schema" in str(exc)
    else:
        raise AssertionError("incomplete material must be refused")


def test_a_module_samples_its_probes_from_a_schema():
    with tempfile.TemporaryDirectory() as work:
        subject = os.path.join(work, "s.py")
        open(subject, "w").write("def entry(args):\n    return args[0]\n")

        module = Module(workspace=work)
        spec = module.specify(_candidate("module", source_path=subject, symbol="scale_values",
                                         schema=_SCALAR, description="d"))
        assert spec.name == "scale-values", "the name comes from the symbol, so runs are comparable"

        drawn = module.probes(spec).draw(5)
        assert len(drawn) == 5 and all(isinstance(args, list) for args in drawn)
        assert module.probes(spec).draw(5) == drawn, "seeded: an expectation must be reproducible"


def test_a_kernel_is_a_module_with_a_numeric_profile():
    """The design claim, checked rather than asserted: a kernel adds three things and no pipeline."""
    with tempfile.TemporaryDirectory() as work:
        subject = os.path.join(work, "s.py")
        open(subject, "w").write("def entry(args):\n    return sum(args[0])\n")

        kernel = Kernel(workspace=work)
        spec = kernel.specify(_candidate("kernel", source_path=subject, symbol="dot",
                                         schema=_NUMERIC, description="d"))

        assert spec.scale == "kernel"
        assert spec.environment["comparison"] == "envelope", "bitwise equality fails a correct rewrite"
        assert spec.environment["cost"] == "wall-clock", "the default, and replaceable"
        assert spec.environment["gpus"] == 0, "GPU is an interface here, not an implementation"

        # Larger shapes than a module's: a kernel's cost belongs in the arithmetic, and at sixteen
        # elements the measurement is dominated by the call.
        assert max(s["n"] for s in kernel.probes(spec).shapes) >= 65536


def test_a_kernel_refuses_material_that_is_not_numeric():
    """A subject with no array is a module wearing the wrong label, and shipping it would make every
    per-scale number afterwards a measurement of a mixture."""
    with tempfile.TemporaryDirectory() as work:
        subject = os.path.join(work, "s.py")
        open(subject, "w").write("def entry(args):\n    return args[0]\n")

        try:
            Kernel(workspace=work).specify(
                _candidate("kernel", source_path=subject, symbol="f", schema=_SCALAR))
        except ValueError as exc:
            assert "no array parameter" in str(exc) and "module rather than a kernel" in str(exc)
        else:
            raise AssertionError("a non-numeric subject must not ship as a kernel")


def test_a_package_will_not_run_a_generator_the_factory_would_have_to_execute():
    """Generators are model-written. Executing one in this process is the rule the pipeline does not
    bend, so a missing container runner is an error rather than a fallback."""
    package = Package()
    spec = package.specify(_candidate(
        "package", entry_points=["a", "b"], generator="def probes(n): ...", description="d"))
    assert spec.environment["comparison"] == "structural"

    try:
        package.probes(spec)
    except RuntimeError as exc:
        assert "inside a container" in str(exc)
    else:
        raise AssertionError("model-written code must not run without a container runner")


def test_a_package_validates_what_a_generator_returned():
    """A generator that returns the wrong shape fails here, naming the probe, rather than deep in a
    freeze where it looks like the subject misbehaved."""
    package = Package(run_generator=lambda source, count: ["not-a-list"])
    spec = package.specify(_candidate("package", entry_points=["a"], generator="x"))

    try:
        package.probes(spec)
    except ValueError as exc:
        assert "probe 0" in str(exc)
    else:
        raise AssertionError("a malformed probe set must be refused")


def test_a_package_names_itself_by_what_is_being_asked():
    rewrite = Package().specify(_candidate("package", entry_points=["a"], generator="x",
                                           target_language="go"))
    faster = Package().specify(_candidate("package", entry_points=["a"], generator="x"))
    assert rewrite.name.endswith("-rewrite") and faster.name.endswith("-faster")


def test_a_repo_needs_scenarios_and_says_why_when_it_has_none():
    """A repository task is graded on commands the project already runs. Without them there is
    nothing to observe, and the refusal should say that rather than produce an empty corpus."""
    repo = Repo()
    spec = repo.specify(_candidate("repo", invoke=["./prog"], root="/tmp", description="d"))

    try:
        repo.probes(spec)
    except ValueError as exc:
        assert "no scenarios were lifted" in str(exc)
    else:
        raise AssertionError("a repository with no scenarios must be refused")


def test_a_cross_language_repo_names_the_target_in_the_task():
    """The name is what a reader sees first, so it carries the thing that makes the task different."""
    spec = Repo().specify(_candidate("repo", invoke=["./rg"], target_language="Zig",
                                     identity="x"))
    assert spec.name.endswith("-zig-rewrite"), spec.name


def test_every_scale_refuses_to_source_without_an_index():
    """No index, no candidates. A scale that could fall back to asking a model for names would have
    an unknowable remaining supply, and a yield against an unknown denominator means nothing."""
    for implementation in (Kernel(), Module(), Package(), Repo()):
        try:
            list(implementation.find(3))
        except LookupError as exc:
            assert "index" in str(exc)
        else:
            raise AssertionError("%s sourced without an index" % implementation.name)

def test_a_batch_serves_each_candidate_its_own_subject():
    """The bug this catches was invisible to every other test in this file.

    They all build ONE task, and the fault needs two: the seam used to capture an observer at
    construction, so a batch built the first candidate's subject and then froze it again for the
    second -- every task after the first describing material it was not built from. With a single
    candidate the captured observer and the fresh one happen to be the same object.

    The other half is that `scale.observe()` must return the SAME observer the seam built through.
    When it did not, the freeze reached an observer with an empty argv and died inside Popen with
    an IndexError that says nothing at all about the cause.
    """
    import tempfile

    from frf.core.scale import Candidate
    from frf.scales import Module

    with tempfile.TemporaryDirectory() as work:
        def candidate(index: int, body: str) -> Candidate:
            path = os.path.join(work, "s%d.py" % index)
            with open(path, "w") as handle:
                handle.write(body)
            return Candidate("test://s%d" % index, "module", "python", "fixture",
                             {"source_path": path, "symbol": "f%d" % index,
                              "schema": {"params": [{"kind": "float_array", "dtype": "float64",
                                                     "size": "n"}]},
                              "description": "d"})

        squares = ("def entry(args):\n"
                   "    values = args[0]\n"
                   "    if not values:\n"
                   "        raise ValueError('empty')\n"
                   "    return sum(x * x for x in values)\n")
        cubes = squares.replace("x * x", "x * x * x")

        scale = Module(workspace=os.path.join(work, "ws"))
        first, second = candidate(1, squares), candidate(2, cubes)

        # Each candidate must be observed through a subject built from ITS OWN source. Asked
        # directly, because the whole failure is that these come out the same.
        answers = []
        for candidate_under_test in (first, second):
            spec = scale.specify(candidate_under_test)
            observer = scale.observe()
            observer.build(spec)
            assert scale.observe() is observer, (
                "observe() handed out a second observer, so whatever the pipeline built is not "
                "what it would freeze")
            with observer.subject(spec) as subject:
                answers.append(subject.call("run", [[1, 2, 3]]).value)

        assert answers == [14, 36], (
            "the second candidate answered %r; a batch is serving one subject for every task"
            % (answers,))
