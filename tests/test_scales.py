"""The four scales, each asked the four questions it exists to answer.

What is checked here is mostly what a scale REFUSES. Each one is a thin adapter between an index and
a shared pipeline, so the interesting behaviour is at its edges: material it should not accept, and
the difference between it and its neighbour.
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf import Factory                                                # noqa: E402
from frf.core.scale import SCALES, Candidate                           # noqa: E402
from frf.scales import Kernel, Module, Package, Repo                   # noqa: E402
from frf.observe.process.observation import freeze                     # noqa: E402
from frf.observe.process.runner import Scenario, Step, run_scenario    # noqa: E402
from frf.scales.repo import (                                          # noqa: E402
    _discover_entrypoint,
    _dockerfile_argv,
    _pyproject_scripts,
    _makefile_has_target,
    _setup_console_script,
    _cargo_package_name,
)

_NUMERIC = {"params": [{"kind": "float_array", "dtype": "float64", "size": "n"}]}
_SCALAR = {"params": [{"kind": "int", "low": 0, "high": 100}]}


def _candidate(scale: str, **detail) -> Candidate:
    return Candidate("test://%s" % scale, scale, "python", "fixture", detail)


def test_all_four_register_and_list_in_the_canonical_order():
    factory = Factory()
    for implementation in (Repo(), Kernel(), Package(), Module()):
        factory.register(implementation)
    assert factory.scales == list(SCALES)



def test_module_sources_from_small_pages_before_building():
    class Index:
        name = "fixture"
        def total(self):
            return 4
        def page(self, number, *, size):
            assert size == 4
            return []

    assert list(Module(index=Index()).find(1)) == []


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


def test_a_repo_harbor_task_replays_its_real_source_in_a_fresh_workspace():
    """E7 uses the emitted reference and fixture, rather than factory-local state."""
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as task:
        program = os.path.join(work, "program")
        with open(program, "w") as handle:
            handle.write("#!/bin/sh\nprintf 'hello %s\\n' \"$1\"\nprintf '%s' \"$1\" > result.txt\n")
        os.chmod(program, 0o755)
        scenario = Scenario("real-source", [Step(["{PROGRAM}", "world"])])
        observed = run_scenario(scenario, [program], exclude=(".git",))
        corpus = SimpleNamespace(scenarios=[scenario],
                                 expectations={scenario.probe_id: [freeze(0, [observed[0]])]})
        repo = Repo()
        repo.specify(_candidate("repo", identity="example/real", root=work,
                                invoke=[program], scenarios=[scenario]))
        repo.write_tests(task, corpus)
        assert os.path.isfile(os.path.join(task, "environment", "program"))
        assert os.path.isfile(os.path.join(task, "tests", "reference", "program"))
        assert os.path.isfile(os.path.join(task, "tests", "verify.py"))
        assert repo.drive(task) == (4, 4)


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

def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_discover_dockerfile_entrypoint_json_form():
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "Dockerfile"),
               'FROM python:3.11\nENTRYPOINT ["python", "-m", "app"]\n')
        build, invoke = _discover_entrypoint(root)
        assert invoke == ["python", "-m", "app"]
        assert build == []


def test_discover_dockerfile_cmd_json_form():
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "Dockerfile"),
               'FROM python:3.11\nCMD ["./server", "--port", "8080"]\n')
        _, invoke = _discover_entrypoint(root)
        assert invoke == ["./server", "--port", "8080"]


def test_dockerfile_bare_shell_ignored():
    """A Dockerfile whose only CMD is /bin/sh should not match."""
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "Dockerfile"),
               'FROM python:3.11\nCMD ["/bin/sh", "-c", "echo hi"]\n')
        _write(os.path.join(root, "main.py"), "print('hi')\n")
        _, invoke = _discover_entrypoint(root)
        # Falls through to main.py since shell is ignored
        assert "main.py" in invoke[-1]


def test_discover_pyproject_scripts():
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "pyproject.toml"),
               '[project]\nname = "myapp"\n[project.scripts]\nmyapp = "myapp.cli:main"\n')
        build, invoke = _discover_entrypoint(root)
        assert invoke == ["myapp"]
        assert any("pip" in str(step) for step in build)


def test_discover_conventional_main_py():
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "main.py"), "print('hello')\n")
        build, invoke = _discover_entrypoint(root)
        assert "python" in invoke[0]
        assert "main.py" in invoke[-1]
        assert build == []


def test_discover_setup_py_console_script():
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "setup.py"),
               "from setuptools import setup\nsetup(name='tool', entry_points={\n"
               "    'console_scripts': ['mytool = tool.main:run'],\n})\n")
        build, invoke = _discover_entrypoint(root)
        assert invoke == ["mytool"]


def test_discover_go_cmd_dir():
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "cmd", "myapp", "main.go"),
               "package main\nfunc main() {}\n")
        build, invoke = _discover_entrypoint(root)
        assert any("go" in str(step) for step in build)
        assert "{ROOT}/program" in invoke


def test_discover_makefile_run_target():
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "Makefile"),
               "build:\n\tgo build .\nrun:\n\t./myapp\n")
        build, invoke = _discover_entrypoint(root)
        assert "make" in invoke
        assert "run" in invoke


def test_discover_no_entry_point_raises_material_error():
    """An empty repository must produce a clear ValueError so the pipeline can attribute MATERIAL."""
    with tempfile.TemporaryDirectory() as root:
        try:
            _discover_entrypoint(root)
        except ValueError as exc:
            assert "no discoverable entry point" in str(exc)
        else:
            raise AssertionError("empty repo must raise ValueError")


def test_dockerfile_argv_parses_shell_form():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(os.path.join(tmp, "Dockerfile"),
                      "FROM python:3.11\nCMD ./server --verbose\n")
        result = _dockerfile_argv(path)
        assert result == ["./server", "--verbose"]


def test_makefile_has_target_detects_present_and_absent():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(os.path.join(tmp, "Makefile"),
                      "build:\n\tgo build .\ntest:\n\tgo test ./...\n")
        assert _makefile_has_target(path, "build")
        assert _makefile_has_target(path, "test")
        assert not _makefile_has_target(path, "run")


def test_locate_fills_invoke_from_main_py_without_network():
    """_locate with empty invoke + root containing main.py populates invoke via discovery."""
    with tempfile.TemporaryDirectory() as root:
        _write(os.path.join(root, "main.py"), "print('hello')\n")
        repo = Repo()
        # Supply root and empty invoke — simulates what GitHub candidate detail looks like.
        cand = _candidate("repo", root=root, invoke=[], description="test", build=[])
        mat = repo._locate(cand)
        assert mat.invoke, "invoke must be populated after discovery"
        assert "python" in mat.invoke[0] or "main.py" in str(mat.invoke)


def test_locate_raises_material_when_no_entry_point_discovered():
    """A repo with nothing discoverable must refuse with ValueError (MATERIAL, not FACTORY)."""
    with tempfile.TemporaryDirectory() as root:
        repo = Repo()
        cand = _candidate("repo", root=root, invoke=[], description="empty", build=[])
        try:
            repo._locate(cand)
        except ValueError as exc:
            assert "no discoverable entry point" in str(exc)
        else:
            raise AssertionError("empty repo must be refused as material")


def test_locate_raises_when_no_repository_url_and_invoke_empty():
    """Empty invoke with no URL and no root must fail with a clear message."""
    repo = Repo()
    cand = _candidate("repo", invoke=[], description="no url")
    try:
        repo._locate(cand)
    except ValueError as exc:
        # Neither a local root nor a URL: there is nothing to discover from, and the refusal is
        # MATERIAL. "no discoverable entry point" is also accepted for a candidate that had a
        # tree but nothing recognisable in it.
        assert ("nothing to discover" in str(exc)
                or "no discoverable entry point" in str(exc))
    else:
        raise AssertionError("must raise ValueError for unresolvable candidate")
