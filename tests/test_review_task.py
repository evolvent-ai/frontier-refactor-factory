from scripts.review_task import review
from frf.core.harbor import Package, task_toml
import json
import pytest


def _task(root):
    (root / "environment").mkdir()
    (root / "tests").mkdir()
    (root / "environment" / "Dockerfile").write_text("FROM python:3.12@sha256:" + "b" * 64 + "\nCOPY . /app\n")
    (root / "task.toml").write_text(task_toml(Package(
        "json-parser-opt", "module", "parser", "Optimize", "python")))
    sections = "\n\n".join(("## Task Facts", "## Workspace", "## Build & Test", "## Constraints",
                                  "## Submission Contract", "## Time Budget", "## Behavioral Rules", "## Grading Signals"))
    (root / "instruction.md").write_text("# JSON Parser Optimization\n\n" + sections)
    (root / "tests" / "expectations.json").write_text("{}")


def test_review_task_accepts_structurally_valid_public_task(tmp_path):
    _task(tmp_path)
    report = review(str(tmp_path))
    assert report["ok"], report


def test_review_task_rejects_repo_without_scenarios(tmp_path):
    _task(tmp_path)
    (tmp_path / "task.toml").write_text(task_toml(Package(
        "repo-opt", "repo", "tool", "Optimize", "python")))
    report = review(str(tmp_path))
    assert not report["ok"] and {x["kind"] for x in report["findings"]} == {"repo-no-scenarios"}


def test_malformed_harbor_task_cannot_pass(tmp_path):
    _task(tmp_path)
    (tmp_path / "task.toml").write_text('name = "anything"\n')
    report = review(str(tmp_path))
    assert not report["ok"]
    assert any(f["kind"] == "invalid-harbor-config" for f in report["findings"])
    assert not report["release_ready"]


def test_cross_label_cannot_pass_with_original_language_workspace(tmp_path):
    _task(tmp_path)
    (tmp_path / 'task.toml').write_text(task_toml(Package(
        'json-parser-to-rust', 'module', 'parser', 'Rewrite', 'python', target_language='rust')))
    (tmp_path / 'environment/task-interface.json').write_text('{"run_command":"/app/run.sh"}')
    findings = {x['kind'] for x in review(str(tmp_path))['findings']}
    assert {'cross-grader-target-mismatch', 'cross-workspace-target-mismatch',
            'cross-implementation-missing', 'cross-reference-runtime-missing'} <= findings
    from frf.observe.in_image import drive
    from types import SimpleNamespace
    def no_upload(*args, **kwargs):
        pytest.fail('invalid cross task reached remote upload')
    result = drive(str(tmp_path), backend=SimpleNamespace(name='remote', push=no_upload))
    assert not result['ok'] and result['stage'] == 'artifact-contract'


@pytest.mark.parametrize('scale', ['module', 'repo'])
def test_timing_partition_cannot_reuse_correctness_probes(tmp_path, scale):
    from scripts.review_task import contract_findings
    (tmp_path / 'tests').mkdir()
    if scale == 'repo':
        (tmp_path / 'tests/expectations.json').write_text('{"one": []}')
        (tmp_path / 'tests/timed.json').write_text('["one", "two"]')
        (tmp_path / 'tests/timed_expectations.json').write_text('{"one": []}')
    else:
        (tmp_path / 'tests/expectations.json').write_text(json.dumps({
            'graded': [{'probe_id': 'one'}], 'timed': ['one', 'two'],
            'timed_expectations': {'one': 'digest'}}))
    findings = contract_findings(tmp_path, {'scale': scale})
    assert {'kind': 'timing-overlaps-correctness', 'probe_ids': ['one']} in findings
    assert {'kind': 'timing-baseline-missing', 'probe_ids': ['two']} in findings


def test_disjoint_frozen_timing_partition_has_no_contradiction(tmp_path):
    from scripts.review_task import contract_findings
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/expectations.json').write_text(json.dumps({
        'graded': [{'probe_id': 'one'}], 'timed': ['two'],
        'timed_expectations': {'two': 'digest'}}))
    assert contract_findings(tmp_path, {'scale': 'module'}) == []
