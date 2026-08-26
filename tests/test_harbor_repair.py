"""Harbor repair must stay outside verifier and reference boundaries."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _repair_task():
    path = Path(__file__).parents[1] / "scripts" / "harbor_check_e2b.py"
    spec = importlib.util.spec_from_file_location("harbor_check_e2b", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.repair_task


def test_repair_only_changes_instruction(tmp_path):
    repair_task = _repair_task()
    instruction = tmp_path / "instruction.md"
    instruction.write_text("# Task\n\nMake it faster.\n", encoding="utf-8")
    protected = {
        "tests/reference/run.sh": "#!/bin/sh\necho reference\n",
        "tests/verify.py": "print('verifier')\n",
        "expectations.json": '{"graded": []}\n',
        "harbor.toml": 'schema_version = "1.4"\n',
    }
    before = {}
    for relative, content in protected.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        before[relative] = path.read_bytes()

    assert repair_task(tmp_path)
    assert "## What you submit" in instruction.read_text(encoding="utf-8")
    for relative, content in before.items():
        assert (tmp_path / relative).read_bytes() == content, relative


def test_repair_is_idempotent_when_instruction_is_complete(tmp_path):
    repair_task = _repair_task()
    path = tmp_path / "instruction.md"
    path.write_text("## What you submit\n\nCreate `/app/run.sh`.\n\n"
                    "## Rules\n\nWork offline.\n", encoding="utf-8")
    original = path.read_bytes()
    assert not repair_task(tmp_path)
    assert path.read_bytes() == original


def test_harbor_check_timeout_is_a_task_failure_not_a_batch_crash(monkeypatch, tmp_path):
    import importlib.util
    import subprocess
    path = Path(__file__).parents[1] / "scripts" / "harbor_run_validate.py"
    spec = importlib.util.spec_from_file_location("harbor_run_validate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("harbor", 30)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    ok, message = module.harbor_check(tmp_path, "harbor")
    assert not ok
    assert "timed out" in message


def test_validator_exposes_schema_only_mode():
    text = (Path(__file__).parents[1] / "scripts" / "harbor_run_validate.py").read_text()
    assert "--schema-only" in text


def test_harbor_agent_bridge_resolves_openai_connection_in_host_process():
    text = (Path(__file__).parents[1] / "scripts" / "harbor_check_e2b.py").read_text()
    assert 'os.environ.setdefault("OPENAI_BASE_URL"' in text
    assert 'os.environ.setdefault("OPENAI_API_KEY"' in text
    assert 'credentials.get("LLM_API_KEY")' in text
    assert 'credentials.get("LLM_BASE_URL")' in text
    assert 'credentials.get("E2B_API_KEY")' in text
    assert 'parser.add_argument("--agent", default="mini-swe-agent")' in text
    assert '"openai/" + args.model' in text
    assert 're.sub(r"[^A-Za-z0-9_-]+", "-", args.job_name)' in text


def test_task_copy_is_after_dockerfile_from_instruction():
    text = (Path(__file__).parents[1] / "scripts" / "harbor_check_e2b.py").read_text()
    assert 'dockerfile.rstrip() + "\\nCOPY task /app/task\\n"' in text
