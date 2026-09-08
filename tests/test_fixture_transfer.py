"""Archive transport preserves legitimate structure without reading links or extracting escapes."""
import io
import subprocess
import tarfile

import pytest

from frf.core.containers import _tar_bytes
from frf.observe.isolated import extract_fixture


def test_image_transfer_retains_dependencies_links_and_empty_directories(tmp_path):
    from frf.observe.in_image import tar_bytes
    dependencies = tmp_path / 'node_modules'
    dependencies.mkdir()
    (dependencies / 'entry.js').write_text('module.exports = 1')
    (tmp_path / 'empty').mkdir()
    (tmp_path / 'linked').symlink_to('node_modules', target_is_directory=True)
    payload = tar_bytes(str(tmp_path))
    assert payload == tar_bytes(str(tmp_path))
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        assert archive.getmember('empty').isdir()
        assert archive.getmember('linked').issym()
        assert archive.extractfile('node_modules/entry.js').read() == b'module.exports = 1'


def test_compressed_transfer_is_deterministic_and_tar_can_read_it(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'empty').mkdir()
    (source / 'data').write_text('repeated content\n' * 10000)
    (source / 'link').symlink_to('data')
    (source / 'dangling').symlink_to('missing')
    (source / 'dirlink').symlink_to('empty', target_is_directory=True)
    payload = _tar_bytes(str(source), compress=True)
    assert payload == _tar_bytes(str(source), compress=True)
    assert len(payload) < len(_tar_bytes(str(source))) / 5
    packed = tmp_path / 'payload.tar'
    packed.write_bytes(payload)
    destination = tmp_path / 'result'
    destination.mkdir()
    subprocess.run(['tar', '-xf', str(packed), '-C', str(destination)], check=True)
    assert (destination / 'empty').is_dir()
    assert (destination / 'data').read_text() == (source / 'data').read_text()
    assert (destination / 'dangling').is_symlink()
    assert (destination / 'dirlink').is_symlink()


def archive(path, members):
    with tarfile.open(path, 'w:gz') as target:
        for name, kind, value in members:
            item = tarfile.TarInfo(name)
            if kind == 'file':
                content = value.encode()
                item.size = len(content)
                item.mode = 0o644
                target.addfile(item, io.BytesIO(content))
            else:
                item.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                item.linkname = value
                target.addfile(item)


def test_fixture_links_and_regular_files_roundtrip(tmp_path):
    packed = tmp_path / 'input.tar.gz'
    archive(packed, [('nested/file', 'file', 'value'), ('soft', 'symlink', 'nested/file'),
                     ('hard', 'hardlink', 'nested/file')])
    root = tmp_path / 'output'
    root.mkdir()
    extract_fixture(packed, root)
    assert (root / 'soft').is_symlink()
    assert (root / 'hard').read_text() == 'value'


@pytest.mark.parametrize('members', [
    [('escape', 'symlink', '../outside'), ('escape/file', 'file', 'bad')],
    [('../outside', 'file', 'bad')],
    [('hard', 'hardlink', '../outside')],
])
def test_fixture_escape_is_rejected(tmp_path, members):
    packed = tmp_path / 'input.tar.gz'
    archive(packed, members)
    root = tmp_path / 'output'
    root.mkdir()
    with pytest.raises((ValueError, FileExistsError)):
        extract_fixture(packed, root)
    assert not (tmp_path / 'outside').exists()


def test_existing_directory_link_cannot_supply_a_hardlink_target(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private').write_text('private')
    root = tmp_path / 'output'
    root.mkdir()
    (root / 'alias').symlink_to(outside, target_is_directory=True)
    packed = tmp_path / 'input.tar.gz'
    archive(packed, [('hard', 'hardlink', 'alias/private')])
    with pytest.raises(ValueError, match='symlink'):
        extract_fixture(packed, root)
    assert not (root / 'hard').exists()
