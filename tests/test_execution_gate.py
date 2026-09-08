"""Deferred isolation remains mandatory and must come from this artifact's real replay."""
import hashlib

import pytest

from frf.core import evidence
from frf.observe.isolated import ISOLATION_CHECKS
from frf.observe.replay import ReplayResult, execution_evidence
from tests.test_factory_interface import _ToyScale, _stages
from frf import Factory


def test_replay_tuple_retains_compatible_counts_and_current_verifier_evidence(tmp_path):
    tests = tmp_path / 'tests'
    tests.mkdir()
    verifier = tests / 'verify.py'
    verifier.write_text('trusted verifier')
    report = {'correct': True, 'correctness_passed': 40, 'correctness_total': 40,
              'timing_valid': True, 'verifier_sha256': hashlib.sha256(verifier.read_bytes()).hexdigest(),
              'isolation': {'enforced': True, 'checks': dict.fromkeys(ISOLATION_CHECKS, True)}}
    replay = ReplayResult(40, 40, report)
    assert replay == (40, 40)
    assert execution_evidence(str(tmp_path), replay).outcome is evidence.Outcome.HOLDS
    report['isolation']['checks']['idle_side_resumed'] = False
    assert not execution_evidence(str(tmp_path), replay).ok
    report['isolation']['checks']['idle_side_resumed'] = True
    verifier.write_text('modified after replay')
    assert not execution_evidence(str(tmp_path), replay).ok
    assert not execution_evidence(str(tmp_path), (40, 40)).ok


@pytest.mark.parametrize('initial', [evidence.Outcome.FAILS, evidence.Outcome.INCONCLUSIVE])
@pytest.mark.parametrize('late', [evidence.Outcome.HOLDS, evidence.Outcome.INCONCLUSIVE,
                                 evidence.Outcome.FAILS, evidence.Outcome.NOT_APPLICABLE, None])
def test_factory_defers_only_inconclusive_and_requires_late_holds(initial, late):
    events = []
    stages = _stages()
    base_battery = stages['battery']
    def battery(*args):
        checks = base_battery(*args)
        checks.record(evidence.Verdict('cannot-delegate-to-the-reference', initial, 'initial'))
        return checks
    stages['battery'] = battery
    stages['emit'] = lambda *args: events.append('emit') or '/tmp/execution-gate-test'
    stages['replay'] = lambda path: events.append('replay') or (40, 40)
    if late is not None:
        def execution(path, result):
            events.append('execution')
            assert events == ['emit', 'replay', 'execution']
            return evidence.Verdict('cannot-delegate-to-the-reference', late, 'late')
        stages['execution_evidence'] = execution
    result = Factory().register(_ToyScale()).install_stages(**stages).build('toy', budget=1)
    succeeds = initial is evidence.Outcome.INCONCLUSIVE and late is evidence.Outcome.HOLDS
    assert bool(result.tasks) is succeeds
    if initial is evidence.Outcome.FAILS or late is None:
        assert events == []
    else:
        assert events == ['emit', 'replay', 'execution']


def test_replay_runtime_failure_is_not_charged_to_source_quality():
    stages = _stages()
    def broken(path):
        raise RuntimeError('reference isolation control did not start')
    stages['replay'] = broken
    result = Factory().register(_ToyScale()).install_stages(**stages).build('toy', budget=1)
    assert not result.tasks
    assert result.batch.refused[0].fault.value == 'factory'


@pytest.mark.parametrize('where', ['probes', 'freeze'])
def test_observation_infrastructure_failure_cannot_inflate_material_refusals(where):
    from frf.core.sandbox import SandboxError
    class Broken(_ToyScale):
        def probes(self, spec):
            if where == 'probes':
                raise SandboxError('remote observation driver timed out')
            return super().probes(spec)
    stages = _stages()
    if where == 'freeze':
        def broken(*args, **kwargs):
            raise SandboxError('remote observation driver timed out')
        stages['freeze'] = broken
    result = Factory().register(Broken()).install_stages(**stages).build('toy', budget=1)
    assert not result.tasks
    assert result.batch.refused[0].fault.value == 'factory'
    assert result.summary()['trustworthy'] is False


@pytest.mark.parametrize('stage', ['unavailable', 'dockerfile', 'changed-artifact', 'replay'])
def test_inconclusive_image_gate_cannot_emit_a_task(stage):
    stages = _stages()
    stages['replay_in_image'] = lambda path: {'ok': False, 'stage': stage, 'total': 0}
    result = Factory().register(_ToyScale()).install_stages(**stages).build('toy', budget=1)
    assert not result.tasks
    assert result.batch.refused[0].fault.value == 'factory'


@pytest.mark.parametrize('changed', ['contents', 'mode', 'link', 'empty-directory'])
def test_image_replay_reuse_requires_unchanged_artifact(tmp_path, monkeypatch, changed):
    from frf.observe import in_image
    from frf.observe.replay import ImageReplay
    tests = tmp_path / 'tests'
    tests.mkdir()
    verifier = tests / 'verify.py'
    verifier.write_text('trusted verifier')
    report = {'correct': True, 'correctness_passed': 4, 'correctness_total': 4,
              'timing_valid': True, 'verifier_sha256': hashlib.sha256(verifier.read_bytes()).hexdigest(),
              'isolation': {'enforced': True, 'checks': dict.fromkeys(ISOLATION_CHECKS, True)}}
    calls = []
    def drive(path, **kwargs):
        calls.append(path)
        return {'ok': True, 'passed': 4, 'total': 4, 'report': report, 'image_id': 'sha256:' + 'a' * 64}
    monkeypatch.setattr(in_image, 'drive', drive)
    replay = ImageReplay(object())
    result = replay.replay(str(tmp_path))
    assert result == (4, 4)
    assert result.report['delivered_image']['image_id'] == 'sha256:' + 'a' * 64
    assert replay.image_check(str(tmp_path))['ok']
    assert len(calls) == 1
    if changed == 'contents':
        verifier.write_text('changed verifier')
    elif changed == 'mode':
        verifier.chmod(0o700)
    elif changed == 'link':
        (tests / 'link').symlink_to('verify.py')
    else:
        (tests / 'fixture-empty').mkdir()
    assert not replay.image_check(str(tmp_path))['ok']
    assert len(calls) == 1
