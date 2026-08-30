"""Language-agnostic entry point discovery, and the danger of guessing.

The repo scale is meant to serve ANY language, and the mechanical manifest paths (pyproject,
Cargo.toml, pom.xml) cover only the languages they name. The language-agnostic paths -- CI
workflows, Justfile, Taskfile, devcontainer -- are what make a genuinely unknown language
servable, because every one of them is the maintainer saying how the project is built and run.

The danger is over-accepting. A CI file is mostly TESTING, and `cargo test` is not an entry
point: the corpus cannot freeze a test runner's output into a deterministic workload, and the
probes stage would refuse it -- after the checkout, E2B upload and build, the three most expensive
stages, have already been paid for. So the tests below check both halves: plausible entry
point commands are found, and commands that can only be a test/lint/build/network step are
refused up front rather than after the build.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.scales.repo import _ci_run_command, _justfile_recipe, _taskfile_command       # noqa: E402


def _with_ci(root: str, command: str) -> bool:
    """-> whether `_ci_run_command` accepts a workflow whose only step runs `command`."""
    workflows = os.path.join(root, ".github", "workflows")
    os.makedirs(workflows, exist_ok=True)
    with open(os.path.join(workflows, "ci.yml"), "w", encoding="utf-8") as handle:
        handle.write("name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                     "    steps:\n      - run: %s\n" % command)
    return _ci_run_command(root) is not None


def test_an_entry_point_command_is_found():
    """`cargo run`, `npm start`, `python main.py` are the shape of a program to freeze."""
    for command in ("cargo run --example demos", "npm run start", "go run ./cmd/x",
                    "python main.py", "make run"):
        with tempfile.TemporaryDirectory() as root:
            assert _with_ci(root, command), (
                "%r is a plausible entry point and must be discovered" % command)


def test_a_test_runner_is_refused_before_the_build_cost_is_paid():
    """`cargo test` is not a program; it will be refused by the probes stage anyway.

    Refusing it here is not an extra gate on top of the probes refusal -- it is the SAME refusal
    moved earlier, before the checkout, upload and build that nothing in the test command could
    ever make use of.
    """
    for command in ("cargo test", "npm test", "go test ./...", "pytest", "make test",
                    "npm run lint", "make check", "docker compose up"):
        with tempfile.TemporaryDirectory() as root:
            assert not _with_ci(root, command), (
                "%r is a test/lint/network command, not an entry point" % command)


def test_a_justfile_recipe_is_discovered():
    """A Justfile recipe is the maintainer's run command, with no language named."""
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "Justfile"), "w", encoding="utf-8") as handle:
            handle.write("run:\n\tcargo run --example demos\n")
        build, invoke = _justfile_recipe(root) or (None, None)
        assert invoke is not None and "just" in invoke[0], invoke


def test_a_taskfile_task_is_discovered():
    """A Taskfile `run` task is the maintainer's command, language-agnostic."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        return  # PyYAML is optional; the discovery silently skips without it
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "Taskfile.yml"), "w", encoding="utf-8") as handle:
            handle.write("version: '3'\ntasks:\n  run:\n    cmds:\n      - python main.py\n")
        _build, invoke = _taskfile_command(root) or (None, None)
        assert invoke is not None and "task" in invoke[0], invoke


def test_a_survey_does_not_carry_the_host_path_into_a_task():
    """`tests/environment.json` is shipped, and it was carrying an absolute host path.

    `RepoSurvey.to_json()` included `root` -- `/…/work/scratch/frf-repo-h6gn63uu` on the machine
    that did the sourcing. Two emitted repo tasks were found carrying it, which told a reader the
    workspace layout, the directory naming and the account that produced the task.

    Nothing downstream reads it: the emitted verifier resolves against the candidate root it is
    handed. Delivery review treats a workspace absolute path in an outbound artefact as blocking on
    its own, so this is asserted rather than left to a reviewer to notice.
    """
    import json

    from frf.source.repo_survey import RepoSurvey

    survey = RepoSurvey(root="/data/somebody/workdir/work/scratch/frf-repo-abcd1234",
                        languages=("go",), build_markers=("go.mod",))
    shipped = survey.to_json()

    assert "root" not in shipped, shipped
    blob = json.dumps(shipped)
    assert "/data/somebody" not in blob and "frf-repo-abcd1234" not in blob, blob
    # The parts the verifier actually needs survive.
    assert shipped["languages"] == ["go"]
    assert shipped["build_markers"] == ["go.mod"]
