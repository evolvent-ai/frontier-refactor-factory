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
    assert 'command -v go >/dev/null 2>&1' in text
    assert 'RUN go mod download' in text
    assert 'reference_program = task_dir / "tests" / "reference" / "program"' in text
    assert 'n_attempts=max(1, args.attempts)' in text
    assert 'semaphore = asyncio.Semaphore(limit)' in text
    assert 'time.time_ns()' in text
    assert 'except Exception as exc:' in text


def test_review_entrypoint_preserves_sibling_results_after_setup_failure(monkeypatch, tmp_path, capsys):
    import json
    import sys
    from types import SimpleNamespace

    namespace = _repair_task().__globals__
    monkeypatch.setattr(namespace['credentials'], 'get', lambda _key: '')
    seen = []

    async def check(task_dir, **_kwargs):
        seen.append(task_dir.name)
        if task_dir.name == 'broken':
            raise RuntimeError('sandbox setup failed')
        result = SimpleNamespace(error=None, model_dump=lambda: {
            'task_name': task_dir.name, 'error': None})
        return SimpleNamespace(results=[result]), tmp_path / 'job'

    monkeypatch.setattr(namespace['checker'], 'run_checks', check)
    for name in ('broken', 'healthy'):
        task = tmp_path / name
        task.mkdir()
        (task / 'task.toml').write_text('')
    monkeypatch.setattr(sys, 'argv', ['harbor_check_e2b.py', str(tmp_path),
                                     '--model', 'test', '--concurrent', '2'])
    assert namespace['main']() == 1
    report = json.loads(capsys.readouterr().out)
    assert sorted(seen) == ['broken', 'healthy']
    assert report['results'] == [
        {'task_name': 'broken', 'error': 'sandbox setup failed'},
        {'task_name': 'healthy', 'error': None},
    ]


def test_review_entrypoint_reports_empty_input(monkeypatch, tmp_path, capsys):
    import json
    import sys

    namespace = _repair_task().__globals__
    monkeypatch.setattr(namespace['credentials'], 'get', lambda _key: '')
    monkeypatch.setattr(sys, 'argv', ['harbor_check_e2b.py', str(tmp_path), '--model', 'test'])
    assert namespace['main']() == 1
    assert json.loads(capsys.readouterr().out) == {
        'results': [], 'error': 'no task.toml files found'}


def test_completed_quality_failure_returns_failure_to_production(monkeypatch, tmp_path, capsys):
    import sys
    from types import SimpleNamespace
    namespace = _repair_task().__globals__
    monkeypatch.setattr(namespace['credentials'], 'get', lambda _key: '')
    monkeypatch.setitem(namespace, '_cap_e2b_timeout', lambda: None)
    (tmp_path / 'task.toml').write_text('')
    checks = {c.name: {'outcome': 'pass', 'explanation': 'reviewed'}
              for c in namespace['checker'].load_rubric().criteria}
    checks['pinned_dependencies']['outcome'] = 'fail'
    async def check(*args, **kwargs):
        item = SimpleNamespace(model_dump=lambda: {'error': None, 'checks': checks})
        return SimpleNamespace(results=[item]), tmp_path
    monkeypatch.setattr(namespace['checker'], 'run_checks', check)
    monkeypatch.setattr(sys, 'argv', ['harbor_check_e2b.py', str(tmp_path), '--model', 'test'])
    assert namespace['main']() == 1
    capsys.readouterr()
    checks['pinned_dependencies']['outcome'] = 'pass'
    assert namespace['main']() == 0
    capsys.readouterr()
    checks.pop('pinned_dependencies')
    assert namespace['main']() == 1
