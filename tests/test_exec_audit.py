import json
import os

import pytest

from frf.observe.isolated import Root


def fake_root(tmp_path):
    root = object.__new__(Root)
    root.closed = False
    root.path = tmp_path
    root.execution_audit = None
    root.execution_logs = []
    return root


def test_missing_or_truncated_execution_log_is_not_complete(tmp_path):
    root = fake_root(tmp_path)
    root.audit_execution()
    root.execution_logs = [tmp_path / 'log']
    assert not root.execution_report()['complete']
    root.execution_logs[0].write_text('{invalid')
    assert not root.execution_report()['complete']
    root.execution_logs[0].write_text(json.dumps({'event': 'exec', 'allowed': True}) + '\n')
    assert not root.execution_report()['complete']


def test_denied_child_cannot_be_hidden_by_a_successful_parent(tmp_path):
    root = fake_root(tmp_path)
    root.audit_execution()
    log = tmp_path / 'log'
    root.execution_logs = [log]
    events = [{'event': 'exec', 'allowed': False},
              {'event': 'complete', 'main_status': 0, 'denied': True}]
    log.write_text('\n'.join(map(json.dumps, events)))
    assert root.execution_report()['denied']
    assert root.execution_report()['complete']


@pytest.mark.parametrize('path', ['relative', '/../../escape'])
def test_execution_allowlist_cannot_reference_outside_its_root(tmp_path, path):
    root = fake_root(tmp_path)
    with pytest.raises(ValueError):
        root.audit_execution([path])


def test_execution_allowlist_rejects_writable_program(tmp_path):
    root = fake_root(tmp_path)
    program = tmp_path / 'program'
    program.write_bytes(b'program')
    program.chmod(0o777)
    with pytest.raises(ValueError, match='immutable'):
        root.audit_execution(['/program'])


@pytest.mark.skipif(os.geteuid() != 0, reason='requires root-owned fixture')
def test_execution_allowlist_is_bound_to_file_identity_and_cannot_be_replaced(tmp_path):
    root = fake_root(tmp_path)
    program = tmp_path / 'program'
    program.write_bytes(b'program')
    program.chmod(0o555)
    root.audit_execution(['/program'])
    stat = program.stat()
    assert root.execution_audit['allowed'] == {(stat.st_dev, stat.st_ino)}
    with pytest.raises(RuntimeError):
        root.audit_execution(['/program'])
