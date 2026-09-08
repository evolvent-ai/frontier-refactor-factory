import json
import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest

from scripts.audit_public_delivery import audit


def make_archive(path, *, extra_tag=False, missing_layer=False):
    config = json.dumps({'os': 'linux', 'architecture': 'amd64', 'rootfs': {'diff_ids': ['sha256:' + 'b' * 64]}}).encode()
    image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
    tag = 'benchmark-runtime:' + image_id[7:]
    manifest = [{'Config': 'config.json', 'RepoTags': [tag] + (['private-org/task:latest'] if extra_tag else []),
                 'Layers': ['layer.tar']}]
    members = {'config.json': config, 'manifest.json': json.dumps(manifest).encode()}
    if not missing_layer:
        members['layer.tar'] = b'layer bytes for metadata validation only'
    with tarfile.open(path, 'w') as archive:
        for name, data in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return image_id


def test_export_archive_metadata_has_anonymous_config_identity(tmp_path):
    from frf.observe.images import archive_metadata
    path = tmp_path / 'image.tar'
    identity = make_archive(path)
    metadata = archive_metadata(path)
    assert metadata['image_id'] == identity
    assert metadata['format'] == 'docker-archive'
    assert metadata['layer_count'] == 1


@pytest.mark.parametrize('tamper', [None, 'config', 'layers'])
def test_containerd_manifest_identity_is_bound_to_its_config(tmp_path, tamper):
    from frf.observe.images import archive_metadata
    config = json.dumps({'os': 'linux', 'architecture': 'amd64', 'rootfs': {'diff_ids': None}}).encode()
    config_id = hashlib.sha256(config).hexdigest()
    descriptor = json.dumps({'schemaVersion': 2, 'config': {'digest': 'sha256:' + config_id}, 'layers': None}).encode()
    identity = hashlib.sha256(descriptor).hexdigest()
    manifest = [{'Config': 'blobs/sha256/' + config_id, 'RepoTags': ['benchmark-runtime:' + identity], 'Layers': None}]
    if tamper == 'config':
        config = config.replace(b'amd64', b'arm64')
    elif tamper == 'layers':
        manifest[0]['Layers'] = ['missing-layer']
    members = {'manifest.json': json.dumps(manifest).encode(),
               'blobs/sha256/' + config_id: config, 'blobs/sha256/' + identity: descriptor}
    path = tmp_path / 'image.tar'
    with tarfile.open(path, 'w') as archive:
        for name, value in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(value)
            archive.addfile(item, io.BytesIO(value))
    if tamper:
        with pytest.raises(ValueError):
            archive_metadata(path)
    else:
        metadata = archive_metadata(path)
        assert metadata['image_id'] == 'sha256:' + identity
        assert metadata['config_id'] == 'sha256:' + config_id
        assert metadata['image_ref'] == 'benchmark-runtime@sha256:' + identity


@pytest.mark.parametrize('fault', ['extra_tag', 'missing_layer'])
def test_archive_refuses_extra_tags_or_missing_layers(tmp_path, fault):
    from frf.observe.images import archive_metadata
    path = tmp_path / 'image.tar'
    make_archive(path, **{fault: True})
    with pytest.raises(ValueError):
        archive_metadata(path)


def test_prepare_task_uses_native_image_identity_and_clears_stale_attestation(tmp_path):
    import tomllib
    from frf.observe.images import prepare_task
    source = tmp_path / 'task'
    (source / 'environment').mkdir(parents=True)
    (source / 'environment/Dockerfile').write_text('FROM old\nRUN apt-get update\n')
    (source / 'task.toml').write_text('[environment]\n[metadata]\nevidence_digest="old"\nsource_language="go"\n')
    identity = 'sha256:' + 'a' * 64
    prepare_task(source, tmp_path / 'public', {'image_id': identity, 'image_tag': 'benchmark-runtime:' + 'a' * 64})
    config = tomllib.loads((tmp_path / 'public/task.toml').read_text())
    assert config['environment']['docker_image'] == 'benchmark-runtime:' + 'a' * 64
    assert 'evidence_digest' not in config['metadata']
    assert config['metadata']['source_language'] == 'go'
    assert (source / 'environment/Dockerfile').read_text().startswith('FROM old')


def test_prepare_task_prefers_verified_manifest_reference(tmp_path):
    import tomllib
    from frf.observe.images import prepare_task
    source = tmp_path / 'source'
    (source / 'environment').mkdir(parents=True)
    (source / 'task.toml').write_text('[environment]\n')
    image = {'image_id': 'sha256:' + 'a' * 64, 'image_tag': 'benchmark-runtime:' + 'a' * 64,
             'image_ref': 'benchmark-runtime@sha256:' + 'a' * 64}
    result = tmp_path / 'public'
    prepare_task(source, result, image)
    assert tomllib.loads((result / 'task.toml').read_text())['environment']['docker_image'] == image['image_ref']
    assert (result / 'environment/Dockerfile').read_text() == 'FROM ' + image['image_tag'] + '\n'


@pytest.mark.parametrize('control', ['task.toml', 'environment/Dockerfile',
                                     'tests/Dockerfile', 'tests/.dockerignore'])
def test_prepare_task_cannot_write_through_source_control_symlinks(tmp_path, control):
    from frf.observe.images import prepare_task
    source = tmp_path / 'source'
    (source / 'environment').mkdir(parents=True)
    (source / 'task.toml').write_text('[environment]\n')
    outside = tmp_path / 'outside'
    outside.write_text('[environment]\n')
    linked = source / control
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.unlink(missing_ok=True)
    linked.symlink_to(outside)
    image = {'image_id': 'sha256:' + 'a' * 64, 'image_tag': 'benchmark-runtime:' + 'a' * 64}
    with pytest.raises(ValueError, match='symbolic'):
        prepare_task(source, tmp_path / 'public', image)
    assert outside.read_text() == '[environment]\n'
    assert not (tmp_path / 'public').exists()


def test_prepared_native_verifier_builds_tests_separately(tmp_path):
    from frf.core import harbor
    from frf.observe.images import prepare_task
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
    from harbor.environments.definition import should_upload_environment_dir, should_use_prebuilt_docker_image

    source = tmp_path / 'source'
    harbor.write(str(source), harbor.Package(
        name='example', scale='repo', description='A task', instruction='A task',
        source_language='go', provenance={}))
    (source / 'tests/test.sh').write_text('#!/bin/sh\nexit 0\n')
    (source / 'tests/.dockerignore').write_text('*\n')
    image = {'image_id': 'sha256:' + 'a' * 64, 'image_tag': 'benchmark-runtime:' + 'a' * 64}
    result = tmp_path / 'public'
    prepare_task(source, result, image)
    config = TaskConfig.model_validate_toml((result / 'task.toml').read_text())
    verifier = resolve_effective_verifier_env_config(config, None)
    assert verifier.docker_image is None
    assert verifier.network_mode == config.environment.network_mode
    assert verifier.cpus == config.environment.cpus
    assert config.verifier.user == 'root'
    assert not should_use_prebuilt_docker_image(result / 'tests', docker_image=verifier.docker_image,
                                                force_build=False)
    assert not should_upload_environment_dir(result / 'tests', docker_image=verifier.docker_image)
    assert (result / 'tests/Dockerfile').read_text() == (
        'FROM ' + image['image_tag'] + '\nUSER root\nCOPY . /tests\nWORKDIR /app\n')
    assert (result / 'tests/.dockerignore').read_text() == ''
    assert (source / 'tests/.dockerignore').read_text() == '*\n'
    assert not (result / 'environment/test.sh').exists()


def test_prepare_refuses_workspace_changed_after_image_export(tmp_path):
    from frf.observe.images import prepare_task
    from frf.observe.replay import _task_digest
    source = tmp_path / 'source'
    (source / 'environment').mkdir(parents=True)
    (source / 'environment/Dockerfile').write_text('FROM image\n')
    (source / 'task.toml').write_text('[environment]\n')
    image = {'image_id': 'sha256:' + 'a' * 64, 'image_tag': 'benchmark-runtime:' + 'a' * 64,
             'source_environment_sha256': _task_digest(source / 'environment')}
    (source / 'environment/program').write_text('changed implementation')
    with pytest.raises(ValueError, match='workspace does not match'):
        prepare_task(source, tmp_path / 'public', image)
    assert not (tmp_path / 'public').exists()


def test_prepared_cross_task_uses_bound_separate_verifier_image(tmp_path):
    import tomllib
    from frf.observe.images import prepare_task
    from frf.observe.replay import _task_digest
    source = tmp_path / 'source'
    (source / 'environment').mkdir(parents=True)
    (source / 'tests').mkdir()
    (source / 'environment/Dockerfile').write_text('FROM solver\n')
    (source / 'tests/verify.py').write_text('trusted evaluator')
    (source / 'tests/reference-runtime.json').write_text('{}')
    (source / 'task.toml').write_text('[environment]\nnetwork_mode="no-network"\n')
    image = {'image_id': 'sha256:' + 'a'*64, 'image_tag': 'benchmark-runtime:'+'a'*64}
    with pytest.raises(ValueError, match='separate exported verifier'):
        prepare_task(source, tmp_path / 'no-verifier', image)
    image['verifier'] = {'image_id':'sha256:'+'b'*64, 'image_tag':'benchmark-runtime:'+'b'*64,
                         'source_tests_sha256':_task_digest(source / 'tests')}
    result = tmp_path / 'public'
    prepare_task(source, result, image)
    config = tomllib.loads((result / 'task.toml').read_text())
    assert config['environment']['docker_image'] == image['image_tag']
    assert config['verifier']['environment']['docker_image'] == image['verifier']['image_tag']
    assert (result / 'tests/Dockerfile').read_text() == 'FROM '+image['verifier']['image_tag']+'\n'
    (source / 'tests/verify.py').write_text('changed evaluator')
    with pytest.raises(ValueError, match='current evaluation files'):
        prepare_task(source, tmp_path / 'stale', image)


def test_export_failure_after_replay_cannot_report_success(tmp_path):
    from frf.observe.in_image import drive
    from frf.core.sandbox import Result
    (tmp_path / 'environment').mkdir()
    (tmp_path / 'environment/Dockerfile').write_text('FROM image\n')
    def run(argv, **kwargs):
        command = argv[-1]
        if command.startswith('docker image inspect'):
            return Result(0, 'sha256:' + 'a' * 64, '')
        if command.startswith('docker run --rm --name'):
            return Result(0, json.dumps({'correctness_passed': 1, 'correctness_total': 1, 'timing_valid': True}), '')
        return Result(0, '', '')
    def fail(*args):
        raise RuntimeError('archive save failed')
    result = drive(str(tmp_path), backend=SimpleNamespace(name='remote', push=lambda *a, **k: None, run=run),
                   image_export=fail)
    assert not result['ok']
    assert result['stage'] == 'image-export'
    assert 'archive save failed' in result['detail']


def test_corrupted_archive_is_rejected_before_allocating_sandbox(tmp_path, monkeypatch):
    from frf.observe import images
    archive = tmp_path / 'image.tar'
    archive.write_bytes(b'corrupt')
    monkeypatch.setattr(images.containers, 'Remote', lambda **kw: pytest.fail('allocated a sandbox'))
    with pytest.raises(ValueError, match='checksum'):
        images.verify_offline(tmp_path, archive, {'archive_sha256': 'a' * 64})


def test_archive_checksum_does_not_waive_offline_build_requirements(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text(
        "FROM python:3.12\nRUN apt-get update\n")
    archive = tmp_path / "task-image.tar"
    archive.write_bytes(b"image")
    import hashlib
    (tmp_path / "task-image.tar.json").write_text(json.dumps({
        "archive": archive.name, "archive_sha256": hashlib.sha256(b"image").hexdigest()}))
    report = audit(str(tmp_path))
    assert report["verified_archive_checksums"] == [archive.name]
    assert not report["ok"]
    assert not report["release_ready"]
    assert {"unpinned-base-image", "networked-build-step"} <= {
        finding["kind"] for finding in report["findings"]}
