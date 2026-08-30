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
from frf.core.scale import SCALES, Candidate, Spec                    # noqa: E402
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
    _maven_main_class,
)
from frf.observe.probes.schema import Schema                              # noqa: E402
from frf.scales.module import ProbeSource, Material as ModuleMaterial, Observer as ModuleObserver, mutate  # noqa: E402

_NUMERIC = {"params": [{"kind": "float_array", "dtype": "float64", "size": "n"}]}
_SCALAR = {"params": [{"kind": "int", "low": 0, "high": 100}]}


def test_module_probe_prefix_contains_semantic_positive_and_negative_cases():
    schema = Schema.from_json({"params": [{"kind": "string"}, {"kind": "string"}]})
    probes = ProbeSource(schema).draw(8)
    assert ["ab", "ba"] in probes
    assert ["ab", "aa"] in probes
    assert ["listen", "silent"] in probes


def test_remote_call_build_uses_backend_instead_of_host_subprocess(tmp_path):
    source = tmp_path / "subject.go"
    source.write_text("package subject\n", encoding="utf-8")

    class Backend:
        name = "remote"
        def __init__(self): self.calls = []
        def push(self, local, remote): self.calls.append(("push", remote))
        def run(self, argv, *, workdir=None, timeout=0, env=None):
            self.calls.append(("run", tuple(argv), workdir))
            from frf.core.sandbox import Result
            return Result(0, "", "")
        def pull(self, remote, local): self.calls.append(("pull", remote))

    backend = Backend()
    material = ModuleMaterial("test", "go", str(source), "entry", "d",
                              Schema.from_json(_NUMERIC))
    observer = ModuleObserver(str(tmp_path), material, backend=backend)
    observer.build(Spec("test", "module", "go", "d"))
    assert [call[0] for call in backend.calls] == ["push", "run", "pull"]


def test_remote_mutant_build_uses_backend_instead_of_host_subprocess(tmp_path):
    source = tmp_path / "subject.ts"
    source.write_text("export function entry(value: number): number { return value + 1; }\n",
                      encoding="utf-8")

    class Backend:
        name = "remote"
        def __init__(self): self.calls = []
        def push(self, local, remote): self.calls.append("push")
        def run(self, argv, *, workdir=None, timeout=0, env=None):
            self.calls.append("run")
            from frf.core.sandbox import Result
            return Result(0, "", "")
        def pull(self, remote, local): self.calls.append("pull")

    backend = Backend()
    material = ModuleMaterial("test", "typescript", str(source), "entry", "d",
                              Schema.from_json(_SCALAR))
    observer = ModuleObserver(str(tmp_path), material, backend=backend)
    observer._argv = ["node"]
    observer._mutant(0)
    assert backend.calls[:2] == ["push", "run"]


def test_rust_src_bin_entrypoint_is_discovered(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "tools"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "cli.rs").write_text("fn main() {}\n", encoding="utf-8")
    build, invoke = _discover_entrypoint(str(tmp_path))
    assert build == [["cargo", "build", "--release", "--bin", "cli"]]
    assert invoke == ["{ROOT}/target/release/cli"]


def test_rust_declared_bin_path_is_discovered(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "tools"\nversion = "0.1.0"\n\n'
        '[[bin]]\nname = "runner"\npath = "tools/runner.rs"\n', encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "runner.rs").write_text("fn main() {}\n", encoding="utf-8")
    build, invoke = _discover_entrypoint(str(tmp_path))
    assert build == [["cargo", "build", "--release", "--bin", "runner"]]
    assert invoke == ["{ROOT}/target/release/runner"]


def test_rust_workspace_member_binary_is_discovered(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["cli"]\n', encoding="utf-8")
    member = tmp_path / "cli"
    (member / "src").mkdir(parents=True)
    (member / "Cargo.toml").write_text(
        '[package]\nname = "cli"\nversion = "0.1.0"\n\n'
        '[[bin]]\nname = "tool"\npath = "src/main.rs"\n', encoding="utf-8")
    (member / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    build, invoke = _discover_entrypoint(str(tmp_path))
    assert build == [["cargo", "build", "--release", "--manifest-path", "{ROOT}/cli/Cargo.toml", "--bin", "tool"]]
    assert invoke == ["{ROOT}/target/release/tool"]


def test_typescript_expression_arrow_gets_a_semantic_mutant():
    source = "export const entry = (value: number): number => value + 1;\n"
    mutant = mutate(source, "typescript", "entry", 0)
    assert mutant != source
    assert "=> null /* mutant */" in mutant
    assert "value + 1" not in mutant


def test_typescript_block_function_has_observable_entry_fallback():
    source = "export function entry(value: number): number { return value + 1; }\n"
    mutant = mutate(source, "typescript", "entry", 1)
    assert "throw new Error('frf mutant')" in mutant


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


def test_java_entrypoint_requires_explicit_maven_main_class(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project><properties><mainClass>com.example.Main</mainClass></properties></project>",
                   encoding="utf-8")
    assert _maven_main_class(str(pom)) == "com.example.Main"
    build, invoke = _discover_entrypoint(str(tmp_path))
    assert build == [["mvn", "-q", "-DskipTests", "-o", "package"]]
    assert invoke == ["java", "-cp", "{ROOT}/target/classes", "com.example.Main"]


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


def test_repo_sourcing_rejects_unpinned_and_oversized_candidates_before_checkout():
    class Index:
        name = "fixture-repos"
        def total(self):
            return 3
        def page(self, number, *, size):
            if number:
                return []
            return [
                Candidate("repo://large", "repo", "python", "fixture",
                          {"repository": "https://example.invalid/large", "commit": "abc",
                           "size_kb": 100_001}),
                Candidate("repo://moving", "repo", "python", "fixture",
                          {"repository": "https://example.invalid/moving"}),
                Candidate("repo://good", "repo", "python", "fixture",
                          {"repository": "https://example.invalid/good", "commit": "abc",
                           "size_kb": 1}),
            ]

    repo = Repo(index=Index())
    # The remaining candidate is accepted by sourcing; checkout is deliberately not reached here.
    found = list(repo.find(3))
    assert [candidate.detail["repository"] for candidate in found] == [
        "https://example.invalid/good"
    ]
    assert repo._index.last_coverage.walked == 3
    assert repo._index.last_coverage.fresh == 1

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


def test_task_names_read_as_one_thing_and_carry_no_revision():
    """A name is read, typed and compared by people; ours spelled identifiers three ways.

    `gonum@8d8e8a102004-faster` put twelve hex digits of revision into the thing a person reads,
    and `interview-BubbleSort` mixed a kebab-case repository with a camel-case symbol. The repo
    scale had already established that the commit belongs in provenance rather than the name; the
    package scale had missed it. The reference benchmarks this factory is measured against are
    uniformly kebab (`cranelift-codegen-opt`, `libexpat-to-x86asm`).
    """
    from frf.scales import module as module_scale
    from frf.scales import package as package_scale

    package = package_scale.Material(
        identity="github:gonum/gonum@8d8e8a102004", language="go", root="",
        entry_points=(), description="")
    assert package_scale._task_name(package) == "gonum-faster"

    rewrite = package_scale.Material(
        identity="github:gonum/gonum@8d8e8a102004", language="go", root="",
        entry_points=(), description="", target_language="rust")
    assert package_scale._task_name(rewrite) == "gonum-rewrite"

    for symbol, expected in (("BubbleSort", "bubble-sort"), ("boyerMoore", "boyer-moore"),
                             ("missing_ranges", "missing-ranges")):
        assert module_scale._slug(symbol) == expected, symbol

    # No emitted name may carry a revision or an upper-case letter.
    for name in (package_scale._task_name(package), package_scale._task_name(rewrite)):
        assert "@" not in name and name == name.lower(), name
