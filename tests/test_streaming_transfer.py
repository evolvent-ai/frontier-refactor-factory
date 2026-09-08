"""Remote archive transfers keep buffers bounded and clean staged data on failure."""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from frf.core.containers import Remote
from frf.core.sandbox import LocalProcess, SandboxError


def test_chunked_upload_and_streamed_download_roundtrip_with_retry(tmp_path, monkeypatch):
    from frf.core import containers
    monkeypatch.setattr(containers.time, 'sleep', lambda seconds: None)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'large').write_bytes(os.urandom(9 * 1024 * 1024))
    (source / 'empty').mkdir()
    remote_dir = tmp_path / 'remote'
    written, readers = [], []
    def write(path, data, **kwargs):
        assert isinstance(data, bytes)
        assert len(data) <= 8 * 1024 * 1024
        assert kwargs['request_timeout'] > 0
        written.append(path)
        Path(path).write_bytes(data)
    @contextmanager
    def read(path, **kwargs):
        assert kwargs['format'] == 'stream'
        assert kwargs['request_timeout'] > 0
        number = len(readers)
        readers.append(False)
        def chunks(handle):
            yield handle.read(65536)
            if number == 0:
                raise TimeoutError('download timeout')
            yield from iter(lambda: handle.read(65536), b'')
        try:
            with open(path, 'rb') as handle:
                yield chunks(handle)
        finally:
            readers[number] = True
    backend = Remote.__new__(Remote)
    backend._sandbox = SimpleNamespace(files=SimpleNamespace(write=write, read=read))
    backend.run = LocalProcess(str(tmp_path)).run
    backend.push(str(source), str(remote_dir))
    assert len(written) == 2
    assert not any(Path(path).exists() for path in written)
    pulled = tmp_path / 'pulled'
    backend.pull(str(remote_dir), str(pulled))
    assert readers == [True, True]
    assert (pulled / 'empty').is_dir()
    assert hashlib.sha256((pulled / 'large').read_bytes()).digest() == hashlib.sha256((source / 'large').read_bytes()).digest()


def test_failed_upload_removes_partial_remote_chunks(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('payload')
    written = []
    def write(path, data, **kwargs):
        written.append(path)
        Path(path).write_bytes(data)
        raise ValueError('upload refused')
    backend = Remote.__new__(Remote)
    backend._sandbox = SimpleNamespace(files=SimpleNamespace(write=write))
    backend.run = LocalProcess(str(tmp_path)).run
    with pytest.raises(SandboxError, match='upload failed'):
        backend.push(str(source), str(tmp_path / 'remote'))
    assert written
    assert not any(Path(path).exists() for path in written)
