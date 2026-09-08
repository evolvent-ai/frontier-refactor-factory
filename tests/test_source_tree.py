"""Source links must not copy or chmod files outside the staged repository."""
import stat
from types import SimpleNamespace

import pytest

from frf.core.source_tree import copy_tree
from frf.scales.repo import Material, Repo, _make_world_readable
from frf.core.scale import Spec


@pytest.mark.parametrize('directory_link', [False, True])
def test_external_link_is_rejected_before_copy(tmp_path, directory_link):
    source = tmp_path / 'source'
    source.mkdir()
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'secret').write_text('private data')
    target = outside if directory_link else outside / 'secret'
    (source / 'leak').symlink_to(target, target_is_directory=directory_link)
    with pytest.raises(ValueError, match='symlink'):
        copy_tree(source, tmp_path / 'copy')
    assert not (tmp_path / 'copy').exists()


def test_relative_internal_links_survive_repeated_staging(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'nested').mkdir()
    (source / 'nested/file').write_text('one')
    (source / 'file-link').symlink_to('nested/file')
    (source / 'directory-link').symlink_to('nested', target_is_directory=True)
    copied = tmp_path / 'copy'
    copy_tree(source, copied)
    (source / 'nested/file').write_text('two')
    copy_tree(source, copied)
    assert (copied / 'file-link').is_symlink()
    assert (copied / 'directory-link').is_symlink()
    assert (copied / 'file-link').read_text() == 'two'


def test_nested_mutation_copy_requires_explicit_destination_exclusion(tmp_path):
    import shutil
    (tmp_path / 'source.py').write_text('source')
    with pytest.raises(ValueError, match='itself'):
        copy_tree(tmp_path, tmp_path / '.mutant-0')
    copy_tree(tmp_path, tmp_path / '.mutant-0', ignore=shutil.ignore_patterns('.mutant-*'))
    assert (tmp_path / '.mutant-0/source.py').read_text() == 'source'
    assert not (tmp_path / '.mutant-0/.mutant-0').exists()


def test_permission_preparation_does_not_follow_external_link(tmp_path):
    private = tmp_path / 'private'
    private.write_text('private data')
    private.chmod(0o600)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'link').symlink_to(private)
    _make_world_readable(str(source))
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_repo_control_writer_does_not_overwrite_link_targets(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'entry.sh').write_text('#!/bin/sh\necho original\n')
    (source / 'run.sh').symlink_to('entry.sh')
    (source / 'metadata.json').write_text('{"original":true}')
    (source / 'task-interface.json').symlink_to('metadata.json')
    task = tmp_path / 'task'
    (task / 'environment').mkdir(parents=True)
    (task / 'environment/Dockerfile').write_text('FROM image\n')
    repo = Repo()
    repo._material = Material('test://source', 'go', str(source), invoke=['./entry.sh'])
    repo._spec = Spec('source-opt', 'repo', 'go', 'Test source controls', invoke=['./entry.sh'])
    repo.write_tests(str(task), SimpleNamespace(expectations={}, scenarios=[], timed=[], timed_expectations={}))
    assert (task / 'environment/entry.sh').read_text() == (source / 'entry.sh').read_text()
    assert (task / 'environment/metadata.json').read_text() == '{"original":true}'
    assert not (task / 'environment/run.sh').is_symlink()
    assert not (task / 'environment/task-interface.json').is_symlink()
