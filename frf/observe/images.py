"""Export verified images remotely and test archive-backed tasks without registry access."""
from __future__ import annotations

import hashlib
from copy import deepcopy
import fcntl
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from ..core import containers, resources, scratch
from .replay import ImageReplay, ReplayResult, execution_evidence, _task_digest


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def archive_metadata(path, *, tarinfo=tarfile.TarInfo):
    """Check archive structure/config identity without extracting layers on the host."""
    with tarfile.open(path, 'r:*', tarinfo=tarinfo) as archive:
        entries = {}
        for member in archive:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or name.as_posix() in entries
                    or len(entries) >= 100000 or not (member.isfile() or member.isdir())):
                raise ValueError('invalid image archive member')
            entries[name.as_posix()] = member

        def read_json(name):
            member = entries.get(name)
            if member is None or not member.isfile() or member.size > 4 * 1024 * 1024:
                raise ValueError('invalid image archive JSON member')
            data = archive.extractfile(member).read()
            return json.loads(data), data

        manifest, _ = read_json('manifest.json')
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise ValueError('exactly one exported image is required')
        image = manifest[0]
        config, data = read_json(image['Config'])
        config_id = 'sha256:' + hashlib.sha256(data).hexdigest()
        tags = image.get('RepoTags')
        if (not isinstance(tags, list) or len(tags) != 1
                or not isinstance(tags[0], str)
                or not re.fullmatch(r'benchmark-runtime:[0-9a-f]{64}', tags[0])):
            raise ValueError('image archive must use only its anonymous content-derived tag')
        image_tag = tags[0]
        image_id = 'sha256:' + image_tag.split(':', 1)[1]
        layers = image.get('Layers') or []
        diff_ids = config.get('rootfs', {}).get('diff_ids') or []
        if (not isinstance(layers, list) or not isinstance(diff_ids, list)
                or len(layers) != len(diff_ids)
                or any(name not in entries or not entries[name].isfile() for name in layers)):
            raise ValueError('image archive is missing declared layers')
        if image_id != config_id:
            # Containerd-backed Docker identifies an image by its manifest, while the classic
            # store uses its config digest. Verify the descriptor graph rather than conflating them.
            def describes_config(identity, depth=0):
                if depth > 4 or not re.fullmatch(r'sha256:[0-9a-f]{64}', identity):
                    return False
                descriptor, blob = read_json('blobs/sha256/' + identity[7:])
                if hashlib.sha256(blob).hexdigest() != identity[7:]:
                    return False
                if descriptor.get('config', {}).get('digest') == config_id:
                    described = descriptor.get('layers') or []
                    return ['blobs/sha256/' + value['digest'].removeprefix('sha256:')
                            for value in described] == layers
                children = descriptor.get('manifests') or []
                return len(children) <= 32 and any(describes_config(value['digest'], depth + 1)
                                                   for value in children)
            if not describes_config(image_id):
                raise ValueError('anonymous image tag is not bound to the archive config and layers')
        if config.get('os') != 'linux' or not config.get('architecture'):
            raise ValueError('unsupported or missing image platform')
        return {'format': 'docker-archive', 'image_id': image_id, 'config_id': config_id, 'image_tag': image_tag,
                'image_ref': 'benchmark-runtime@' + image_id if image_id != config_id else None,
                'platform': {'os': config['os'], 'architecture': config['architecture']},
                'layer_count': len(layers)}


def _checked(backend, argv, timeout=60):
    result = backend.run(argv, timeout=timeout)
    if not result.ok:
        raise RuntimeError('remote image operation failed: %s' % result.tail(1500))
    return result


def export(task_dir, output, *, backend=None, log=lambda _message: None):
    """Build/replay/save in E2B; commit only after replay and cleanup pass."""
    root, archive = Path(task_dir).resolve(), Path(output).resolve()
    if not (root / 'environment/Dockerfile').is_file():
        raise ValueError('task has no environment/Dockerfile')
    if archive.is_relative_to(root):
        raise ValueError('image archive must be outside the task build tree')
    metadata_path = archive.with_suffix(archive.suffix + '.json')
    audit_path = archive.parent / '.frf-evidence' / (archive.name + '.audit.json')
    verifier_archive = archive.with_name(archive.stem + '.verifier' + archive.suffix)
    if audit_path.parent.is_symlink():
        raise ValueError('image audit evidence directory must not be a symbolic link')
    if archive.exists() or metadata_path.exists() or audit_path.exists():
        raise FileExistsError('image export destination already exists')
    archive.parent.mkdir(parents=True, exist_ok=True)
    resources.require_headroom(archive.parent)
    lock = archive.with_suffix(archive.suffix + '.exporting')
    descriptor = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise FileExistsError('image export is already running')
    own_backend = backend is None
    try:
        if backend is None:
            backend = containers.Remote(timeout=3600)
        if getattr(backend, 'name', '') != 'remote':
            raise ValueError('image export must use E2B')
        with tempfile.TemporaryDirectory(prefix='.image-export-', dir=archive.parent) as local:
            staged_archive = Path(local) / 'image.tar'

            def save(image_id, report, artifact='solver'):
                folder = Path(local) if artifact == 'solver' else Path(local) / 'verifier'
                folder.mkdir(exist_ok=True)
                staged_archive = folder / 'image.tar'
                output_archive = archive if artifact == 'solver' else verifier_archive
                if output_archive.exists():
                    raise FileExistsError(output_archive)
                verdict = execution_evidence(str(root), ReplayResult(
                    report['correctness_passed'], report['correctness_total'], report))
                if not verdict.ok:
                    raise RuntimeError(verdict.detail)
                if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
                    raise ValueError('invalid verified image identity')
                tag = 'benchmark-runtime:' + image_id.removeprefix('sha256:')
                remote = '/tmp/image-export-' + uuid.uuid4().hex
                tagged = False
                try:
                    _checked(backend, ['mkdir', '-p', remote])
                    _checked(backend, ['docker', 'tag', image_id, tag])
                    tagged = True
                    log('image export: saving verified image in E2B')
                    _checked(backend, ['docker', 'save', '-o', remote + '/image.tar', tag], timeout=900)
                    expected = _checked(backend, ['sha256sum', remote + '/image.tar'], timeout=300).stdout.split()[0]
                    backend.pull(remote, str(folder))
                    with resources.transfer_slot(archive.parent):
                        actual = file_digest(staged_archive)
                        info = archive_metadata(staged_archive)
                    if actual != expected or info['image_id'] != image_id:
                        raise RuntimeError('exported archive does not match its verified image')
                    from .image_audit import audit_image
                    from ..core import credentials
                    secrets = [credentials.get(name) for name in
                               ('LLM_API_KEY', 'E2B_API_KEY', 'GITHUB_TOKEN')]
                    with resources.transfer_slot(archive.parent):
                        audit_report = audit_image(staged_archive, secret_values=secrets, log=log)
                    (folder / 'audit.json').write_text(json.dumps(audit_report, indent=2) + '\n')
                    if not audit_report['complete'] or not audit_report['ok']:
                        audit_path.parent.mkdir(exist_ok=True)
                        failed_audit = audit_path.parent / (output_archive.name + '.failed-audit-' + uuid.uuid4().hex + '.json')
                        os.link(folder / 'audit.json', failed_audit)
                        raise RuntimeError('exported image layer audit did not pass (%d unresolved findings); %s'
                                           % (len(audit_report['findings']), failed_audit))
                    info = dict(info, archive=output_archive.name, archive_sha256=actual,
                                bytes=staged_archive.stat().st_size)
                    if artifact == 'solver' and report.get('verifier_image_id'):
                        info['verifier'] = save(report['verifier_image_id'], report, artifact='verifier')
                    return info
                finally:
                    try:
                        _checked(backend, ['rm', '-rf', remote])
                    finally:
                        if tagged:
                            _checked(backend, ['docker', 'image', 'rm', '-f', tag])

            replay = ImageReplay(backend, log=log, image_export=save)
            replay.replay(str(root))
            outcome = replay.image_check(str(root))
            if not outcome['ok']:
                raise RuntimeError('task changed after verified image export')
            info = outcome['image_export']
            info['source_task_sha256'] = _task_digest(root)
            info['source_environment_sha256'] = _task_digest(root / 'environment')
            if info.get('verifier'):
                info['verifier']['source_tests_sha256'] = _task_digest(root / 'tests')
            staged_metadata = Path(local) / 'metadata.json'
            staged_metadata.write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
            audit_path.parent.mkdir(exist_ok=True)
            staged_audit = Path(local) / 'audit.json'
            commits = [(staged_archive, archive), (staged_audit, audit_path)]
            if info.get('verifier'):
                verifier_metadata = Path(local) / 'verifier/metadata.json'
                verifier_metadata.write_text(json.dumps(info['verifier'], indent=2, sort_keys=True) + '\n')
                commits += [(Path(local) / 'verifier/image.tar', verifier_archive),
                            (Path(local) / 'verifier/audit.json', audit_path.parent / (verifier_archive.name + '.audit.json')),
                            (verifier_metadata, verifier_archive.with_suffix(verifier_archive.suffix + '.json'))]
            commits.append((staged_metadata, metadata_path))
            committed = []
            try:
                for source_file, target_file in commits:
                    os.link(source_file, target_file)
                    committed.append((source_file, target_file))
            except BaseException:
                for source_file, target_file in reversed(committed):
                    if target_file.stat().st_ino == source_file.stat().st_ino:
                        target_file.unlink()
                raise
            return info
    finally:
        try:
            if own_backend and backend is not None:
                backend._sandbox.kill(request_timeout=30)
        finally:
            try:
                lock.unlink(missing_ok=True)
            finally:
                os.close(descriptor)


def prepare_task(task_dir, destination, image):
    """Use Harbor's native image field and retain an offline build recipe."""
    import toml
    source = Path(task_dir)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    for path in (source / 'task.toml', source / 'environment', source / 'environment/Dockerfile',
                 source / 'tests', source / 'tests/Dockerfile', source / 'tests/.dockerignore'):
        if path.is_symlink():
            raise ValueError('task control paths must not be symbolic links')
    identity = image.get('image_id')
    if (not isinstance(identity, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', identity)
            or image.get('image_tag') != 'benchmark-runtime:' + identity[7:]):
        raise ValueError('image identity and anonymous tag disagree')
    if image.get('image_ref') and image['image_ref'] != 'benchmark-runtime@' + identity:
        raise ValueError('image reference and archive identity disagree')
    reference = image.get('image_ref') or image['image_tag']
    verifier_image = image.get('verifier')
    if verifier_image:
        verifier_identity = verifier_image.get('image_id')
        if (not isinstance(verifier_identity, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', verifier_identity)
                or verifier_image.get('image_tag') != 'benchmark-runtime:' + verifier_identity[7:]
                or verifier_image.get('image_ref') not in (None, 'benchmark-runtime@' + verifier_identity)):
            raise ValueError('verifier image identity is invalid')
        if verifier_image.get('source_tests_sha256') != _task_digest(source / 'tests'):
            raise ValueError('verifier image does not match current evaluation files')
    elif (source / 'tests/reference-runtime.json').exists():
        raise ValueError('cross task requires its separate exported verifier image')
    source_digest = image.get('source_environment_sha256')
    if source_digest and source_digest != _task_digest(source / 'environment'):
        raise ValueError('source workspace does not match the exported image')
    shutil.copytree(task_dir, destination, symlinks=True)
    config_path = destination / 'task.toml'
    config = toml.loads(config_path.read_text())
    # The checked archive tag works with both classic and containerd Docker image stores.
    config.setdefault('environment', {})['docker_image'] = reference
    verifier = config.setdefault('verifier', {})
    verifier['environment_mode'] = 'separate'
    verifier['user'] = 'root'
    verifier_environment = deepcopy({**config['environment'], **verifier.get('environment', {})})
    # Harbor 0.22 separate verification does not upload tests after startup. Build a
    # distinct verifier image with /tests; inheriting the agent image skips that build.
    verifier_environment.pop('docker_image', None)
    if verifier_image:
        verifier_environment['docker_image'] = verifier_image.get('image_ref') or verifier_image['image_tag']
    verifier['environment'] = verifier_environment
    # Previous attestations describe the factory artifact, not this archive-backed package.
    from ..core.attestation import SUMMARY_KEYS
    for key in SUMMARY_KEYS:
        config.get('metadata', {}).pop(key, None)
    config_path.write_text(toml.dumps(config))
    # BuildKit still resolves registry metadata for imported repo@digest references. The
    # archive-bound local tag supports offline builds; Harbor can directly run the digest ref.
    (destination / 'environment/Dockerfile').write_text('FROM ' + image['image_tag'] + '\n')
    tests = destination / 'tests'
    tests.mkdir(exist_ok=True)
    recipe = ('FROM ' + verifier_image['image_tag'] + '\n') if verifier_image else (
        'FROM ' + image['image_tag'] + '\nUSER root\nCOPY . /tests\nWORKDIR /app\n')
    (tests / 'Dockerfile').write_text(recipe)
    (tests / '.dockerignore').write_text('')
    return str(destination)


def verify_offline(task_dir, archive_path, image, *, log=lambda _message: None):
    """Import in fresh E2B, block outbound network, build and replay the task."""
    archive_path = Path(archive_path).resolve()
    archives = [(archive_path, image, 'image.tar')]
    if image.get('verifier'):
        metadata = image['verifier']
        companion = archive_path.parent / str(metadata.get('archive', ''))
        if companion.is_symlink() or companion.resolve().parent != archive_path.parent:
            raise ValueError('verifier archive must be next to its solver archive')
        archives.append((companion, metadata, 'verifier.tar'))
    with resources.transfer_slot(archive_path.parent):
        for source, metadata, _name in archives:
            if file_digest(source) != metadata['archive_sha256']:
                raise ValueError('image archive checksum mismatch')
            if archive_metadata(source)['image_id'] != metadata['image_id']:
                raise ValueError('image archive identity mismatch')
    backend = containers.Remote(timeout=2400)
    record = {'sandbox_id': backend._sandbox.sandbox_id, 'ok': False}
    try:
        with scratch.temporary_directory() as local:
            for source, _metadata, name in archives:
                try:
                    os.link(source, Path(local) / name)
                except OSError:
                    resources.require_headroom(local, disk_bytes=source.stat().st_size)
                    shutil.copyfile(source, Path(local) / name)
            backend.push(local, '/tmp/image-import')
        connected = _checked(backend, ['python3', '-I', '-c',
            'import socket; s=socket.create_connection(("1.1.1.1",443),timeout=5); s.close(); print("CONNECTED")'], timeout=15)
        if connected.stdout.strip() != 'CONNECTED':
            raise RuntimeError('offline network control could not establish its positive case')
        record['outbound_control_connected'] = True
        # Preserve established control connections; scorer containers also use --network=none.
        for tool in ('iptables', 'ip6tables'):
            _checked(backend, [tool, '-I', 'OUTPUT', '1', '-m', 'conntrack', '--ctstate',
                               'ESTABLISHED,RELATED', '-j', 'ACCEPT'])
            _checked(backend, [tool, '-I', 'OUTPUT', '2', '-o', 'lo', '-j', 'ACCEPT'])
            _checked(backend, [tool, '-I', 'OUTPUT', '3', '-j', 'REJECT'])
        denied = backend.run(['python3', '-I', '-c',
            'import socket\ntry: socket.create_connection(("1.1.1.1",443),timeout=3)\n'
            'except OSError: print("BLOCKED")\nelse: raise RuntimeError("outbound connection succeeded")'], timeout=10)
        if not denied.ok or denied.stdout.strip() != 'BLOCKED':
            raise RuntimeError('offline network control did not deny outbound connections')
        record['outbound_denied'] = True
        _checked(backend, ['docker', 'load', '-i', '/tmp/image-import/image.tar'], timeout=600)
        if image.get('verifier'):
            _checked(backend, ['docker', 'load', '-i', '/tmp/image-import/verifier.tar'], timeout=600)
            verifier_loaded = _checked(backend, ['docker', 'image', 'inspect', '--format', '{{.Id}}', image['verifier']['image_tag']])
            if verifier_loaded.stdout.strip() not in (image['verifier']['image_id'], image['verifier'].get('config_id')):
                raise RuntimeError('loaded verifier image does not have its declared identity')
            record['loaded_verifier_image_id'] = verifier_loaded.stdout.strip()
        loaded = _checked(backend, ['docker', 'image', 'inspect', '--format', '{{.Id}}', image['image_tag']])
        if loaded.stdout.strip() not in (image['image_id'], image.get('config_id')):
            raise RuntimeError('loaded image does not have its declared identity')
        record['loaded_image_id'] = loaded.stdout.strip()
        if image.get('image_ref'):
            pinned = _checked(backend, ['docker', 'image', 'inspect', '--format', '{{.Id}}', image['image_ref']])
            if pinned.stdout.strip() != record['loaded_image_id']:
                raise RuntimeError('imported digest reference does not resolve to the loaded image')
            record['loaded_image_ref'] = image['image_ref']
        log('image import: identity matched; checking offline build and replay')
        replay = ImageReplay(backend, log=log, offline=True)
        result = replay.replay(str(task_dir))
        record.update(ok=True, report=result.report, image_check=replay.image_check(str(task_dir)))
        return record
    finally:
        backend._sandbox.kill(request_timeout=30)
        record['teardown_ok'] = True
