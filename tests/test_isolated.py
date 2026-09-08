"""Offline checks for trusted runtime staging; process confinement is checked in E2B."""
import os
from types import SimpleNamespace

import pytest

from frf.observe.isolated import Root, _copy_runtime


def test_nonroot_verifier_and_root_subject_are_refused(monkeypatch):
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    with pytest.raises(PermissionError):
        Root('/unused', uid=50001)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError):
        Root('/unused', uid=0)


@pytest.mark.parametrize('uid,mode', [(1000, 0o644), (1000, 0o666)])
def test_mutable_or_user_owned_runtime_is_not_shared(tmp_path, monkeypatch, uid, mode):
    source = tmp_path / 'library'
    target = tmp_path / 'target'
    source.write_text('runtime')
    real_stat = os.stat
    monkeypatch.setattr(os, 'stat', lambda path, **kwargs: (
        SimpleNamespace(st_uid=uid, st_mode=mode) if path == source else real_stat(path, **kwargs)))
    with pytest.raises(ValueError, match='untrusted'):
        _copy_runtime(source, target)
    assert not target.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason='requires a root-owned runtime fixture')
def test_root_owned_writable_runtime_is_copied_and_sealed(tmp_path):
    source = tmp_path / 'runtime'
    target = tmp_path / 'sealed'
    source.write_text('official toolchain bytes')
    source.chmod(0o777)
    _copy_runtime(source, target)
    assert source.stat().st_ino != target.stat().st_ino
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o022 == 0
    assert source.stat().st_mode & 0o022 == 0o022


def test_closed_root_cannot_control_a_later_subject_reusing_its_uid():
    root = object.__new__(Root)
    root.closed = True
    for operation in (root.pause, root.resume, root.prepare, lambda: root.spawn(['unused'])):
        with pytest.raises(RuntimeError, match='closed'):
            operation()


def test_broken_pipe_does_not_skip_root_cleanup(tmp_path, monkeypatch):
    from frf.observe import isolated
    root = object.__new__(Root)
    root.closed, root.uid, root.room = False, 50001, tmp_path
    def broken_close():
        raise BrokenPipeError('subject exited before reading input')
    root.processes = [SimpleNamespace(wait=lambda **kwargs: 0,
                                      stdin=SimpleNamespace(close=broken_close),
                                      stdout=None, stderr=None)]
    monkeypatch.setattr(isolated, '_processes_for', lambda *args, **kwargs: iter(()))
    root.close()
    assert root.closed and not tmp_path.exists()
