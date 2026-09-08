"""Replay must validate frozen evidence and preserve failures across execution layers."""
import json
import hashlib
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from frf.core.sandbox import Result
from frf.observe import in_image
from frf.observe.call import package
from frf.observe.isolated import ISOLATION_CHECKS
from frf.scales.repo import Repo
from tests.test_timing_protocol import load_verifier


def test_missing_process_submission_retains_all_graded_points(tmp_path, monkeypatch):
    verifier = load_verifier(tmp_path, monkeypatch, 'process')
    rules = {name: {'graded': name != 'stderr', 'digest': 'sha256:' + '0' * 64,
                    'line_count': 0} for name in ('exit_code', 'stdout', 'stderr', 'tree')}
    files = {
        'scenarios.jsonl': json.dumps({'probe_id': 'work', 'steps': [
            {'argv': ['{PROGRAM}']}, {'argv': ['{PROGRAM}', 'next']}]}) + '\n',
        'expectations.json': json.dumps({'work': [rules, rules]}),
        'timed.json': '[]', 'environment.json': '{}',
    }
    for name, value in files.items():
        (tmp_path / name).write_text(value)
    reward = tmp_path / 'reward.json'
    result = subprocess.run([sys.executable, verifier.__file__], capture_output=True, text=True,
                            env=dict(os.environ, SUBMISSION_ROOT=str(tmp_path / 'missing'),
                                     REWARD_PATH=str(reward)), timeout=20)
    assert result.returncode == 1, result.stderr
    report = json.loads(reward.read_text())
    assert report['correctness_passed'] == 0
    assert report['correctness_total'] == 6
    assert report['reward'] == 0


def test_legacy_refresh_cannot_rewrite_wrong_frozen_answers(tmp_path, monkeypatch):
    verifier = load_verifier(tmp_path, monkeypatch, "process")
    reference = tmp_path / "reference"
    reference.mkdir()
    launcher = reference / "run.sh"
    launcher.write_text("#!/bin/sh\nprintf 'actual output\\n'\n")
    launcher.chmod(0o755)
    scenario = {"probe_id": "work", "steps": [{"argv": ["{PROGRAM}", "input"]}]}
    observed = verifier.run_scenario(scenario, [str(launcher)], "", ())[0]
    rules = {}
    for channel, value in observed.items():
        digest, count = verifier.stream_digest(str(value))
        rules[channel] = {"graded": True, "digest": digest, "line_count": count}
    rules["stdout"]["digest"] = "sha256:" + "0" * 64
    files = {
        "scenarios.jsonl": json.dumps(scenario) + "\n",
        "expectations.json": json.dumps({"work": [rules]}),
        "timed.json": json.dumps(["work"]),
        "timed_expectations.json": json.dumps({"work": [rules]}),
        "environment.json": json.dumps({"isolated": False}),
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    before = {name: (tmp_path / name).read_bytes() for name in files}
    reward = tmp_path / "reward.json"
    done = subprocess.run(
        [sys.executable, verifier.__file__], capture_output=True, text=True, timeout=20,
        env=dict(os.environ, FRF_REFRESH_EXPECTATIONS="1", REWARD_PATH=str(reward),
                 SUBMISSION_ROOT=str(reference)))
    assert done.returncode in (0, 1), done.stderr
    report = json.loads(reward.read_text())
    assert report["correct"] is False
    assert report["correctness_passed"] < report["correctness_total"]
    assert {name: (tmp_path / name).read_bytes() for name in files} == before


@pytest.mark.parametrize("kind", ["repo", "call"])
@pytest.mark.parametrize("status,report,accepted", [
    (0, {"correctness_passed": 4, "correctness_total": 4, "timing_valid": True}, True),
    (7, {"correctness_passed": 4, "correctness_total": 4, "timing_valid": True}, False),
    (0, {"correctness_passed": 4, "correctness_total": 4, "timing_valid": False}, False),
    (0, {"correctness_passed": 4, "correctness_total": 4}, False),
    (0, {"correctness_passed": 3, "correctness_total": 4}, False),
    (0, {"correctness_passed": 0, "correctness_total": 0}, False),
    (0, None, False),
])
def test_remote_replay_never_refreezes_or_falls_back(tmp_path, monkeypatch,
                                                  kind, status, report, accepted):
    calls = []
    (tmp_path / 'environment').mkdir()
    (tmp_path / 'environment/Dockerfile').write_text('FROM scratch\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/verify.py').write_text('trusted verifier')
    (tmp_path / 'task.toml').write_text('scale = "' + ('repo' if kind == 'repo' else 'module') + '"\n')
    if report is not None:
        report = dict(report, correct=True,
                      verifier_sha256=hashlib.sha256(b'trusted verifier').hexdigest(),
                      isolation={'enforced': True, 'checks': dict.fromkeys(ISOLATION_CHECKS, True)})

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv[:2] == ['sh', '-c']
        command = argv[2]
        if command.startswith('docker image inspect '):
            return Result(0, 'sha256:' + 'a' * 64, '')
        if '--entrypoint python3' in command:
            return Result(0, '{"missing":[]}', '')
        if command.startswith('docker run '):
            return Result(status, json.dumps(report) if report else '', '')
        return Result(0, "", "")

    def forbidden(*args, **kwargs):
        pytest.fail("remote replay attempted host execution or reference replacement")

    backend = SimpleNamespace(name="remote", run=run, pull=forbidden,
                              push=lambda *args, **kwargs: None)
    repo = Repo(backend=backend)
    repo._built = SimpleNamespace(_backend=backend)
    monkeypatch.setattr(subprocess, "run", forbidden)
    drive = repo.drive if kind == "repo" else lambda path: package.drive(path, backend=backend)
    if accepted:
        assert drive(str(tmp_path)) == (4, 4)
    else:
        with pytest.raises(RuntimeError):
            drive(str(tmp_path))
    execution = next(argv[2] for argv, _ in calls if argv[2].startswith('docker run --rm --name '))
    assert '--user 0 --network=none' in execution
    assert 'sha256:' + 'a' * 64 in execution
    assert 'tests/reference' in execution
    assert calls[-1][0][2].startswith('rm -rf /tmp/frf-in-image-')


@pytest.mark.parametrize("scale", ["repo", "module"])
@pytest.mark.parametrize("status", [0, 7])
@pytest.mark.parametrize("report_channel", ["stdout", "reward-file"])
def test_image_replay_preserves_status_through_both_shells(tmp_path, monkeypatch, scale, status,
                                                         report_channel):
    # Execute the actual nested shell command with tiny stand-ins for docker and the verifier.
    # A JSON-quoted command expands $status in the outer shell and makes the failure disappear.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    report = json.dumps({"correctness_passed": 4, "correctness_total": 4, "timing_valid": True})
    scripts = {
        "docker": '#!/bin/sh\nwhile [ "$1" != sh ]; do shift; done\nexec "$@"\n',
        "python3": "#!/bin/sh\nprintf '%s\\n' '" + report + "'\nexit " + str(status) + "\n",
        "cat": "#!/bin/sh\nexit 0\n",
    }
    if report_channel == 'reward-file':
        monkeypatch.setenv('REWARD_TEST_FILE', str(tmp_path / 'private-reward.json'))
        scripts['python3'] = ("#!/bin/sh\nprintf '%s\\n' '" + report +
                              "' > \"$REWARD_TEST_FILE\"\nexit " + str(status) + "\n")
        scripts['cat'] = ('#!/bin/sh\n[ "$1" = /tmp/reward.json ] || exit 3\n'
                          'exec /bin/cat "$REWARD_TEST_FILE"\n')
    for name, content in scripts.items():
        path = bindir / name
        path.write_text(content)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("status", raising=False)
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "task.toml").write_text('scale = "' + scale + '"\n')
    commands = []
    closed = []

    def run(command, **kwargs):
        commands.append(command)
        if command.startswith('docker image inspect '):
            return SimpleNamespace(exit_code=0, stdout='sha256:' + 'a' * 64, stderr='')
        if '--entrypoint python3' in command:
            return SimpleNamespace(exit_code=0, stdout='{"missing":[]}', stderr='')
        if command.startswith("docker run "):
            done = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=10)
            return SimpleNamespace(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    sandbox = SimpleNamespace(commands=SimpleNamespace(run=run),
                              files=SimpleNamespace(write=lambda *a, **kw: None),
                              kill=lambda **kw: closed.append(True))
    monkeypatch.setitem(sys.modules, "e2b", SimpleNamespace(
        Sandbox=SimpleNamespace(create=lambda **kwargs: sandbox)))
    monkeypatch.setattr(in_image, "tar_bytes", lambda path: b"")
    result = in_image.drive(str(tmp_path), api_key="test", template="test")
    assert result["ok"] is (status == 0), result
    replay = next(command for command in commands if command.startswith("docker run --rm --name "))
    assert "--network=none" in replay
    if scale == "repo":
        assert "SUBMISSION_ROOT=/task/tests/reference" in replay
    assert closed == [True]
