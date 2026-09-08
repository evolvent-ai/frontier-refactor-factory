import gzip
import hashlib
import io
import json
import tarfile

import pytest

from frf.core.public_scan import scan_bytes
from frf.observe.image_audit import audit_image


def make_layer(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            if isinstance(data, tuple):
                info.type = tarfile.SYMTYPE
                info.linkname = data[0]
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def make_image(path, layers, *, history=None, tamper=False, compress=True):
    config = json.dumps({'os': 'linux', 'architecture': 'amd64', 'history': history or [],
                         'rootfs': {'diff_ids': ['sha256:' + hashlib.sha256(x).hexdigest()
                                                 for x in layers]}}).encode()
    identity = hashlib.sha256(config).hexdigest()
    names = ['layer%d.tar' % i for i in range(len(layers))]
    manifest = [{'Config': 'config.json', 'RepoTags': ['benchmark-runtime:' + identity], 'Layers': names}]
    members = {'config.json': config, 'manifest.json': json.dumps(manifest).encode()}
    for name, data in zip(names, layers):
        if tamper:
            data = data.replace(b'upstream', b'altered!')
        members[name] = gzip.compress(data) if compress else data
    with tarfile.open(path, 'w') as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize('compress', [False, True])
def test_image_audit_checks_every_layer_and_all_file_content(tmp_path, compress):
    path = tmp_path / 'image.tar'
    make_image(path, [make_layer({'app/LICENSE': b'public upstream license',
                                 'app/run': (('/usr/bin/python3'),),
                                 'app/data': b'clean data'})], compress=compress)
    report = audit_image(path)
    assert report['ok'] and report['complete']
    assert report['members'] == 3
    assert report['uncompressed_bytes'] == 10240
    assert not report['release_ready']


def test_deleted_secret_is_still_found_in_historical_layer(tmp_path):
    path = tmp_path / 'image.tar'
    secret = 'private-canary-value-' + 'z' * 40
    make_image(path, [make_layer({'app/credential': secret.encode()}),
                     make_layer({'app/.wh.credential': b''})])
    report = audit_image(path, secret_values=[secret])
    assert report['complete'] and not report['ok']
    assert any(x['layer'] == 0 and x['kind'] == 'configured-secret' for x in report['findings'])
    assert secret not in json.dumps(report)


def test_history_and_link_targets_are_scanned(tmp_path):
    path = tmp_path / 'image.tar'
    make_image(path, [make_layer({'app/link': ('/data/evolvent/private',)})],
               history=[{'created_by': 'COPY /data/evolvent/task /app'}])
    report = audit_image(path)
    assert report['complete'] and not report['ok']
    assert {x['kind'] for x in report['findings']} == {'internal-identity'}
    assert any(x.get('archive_member') == 'config.json' for x in report['findings'])
    assert any(x.get('path') == 'app/link' for x in report['findings'])


def test_layer_diff_id_mismatch_fails_closed(tmp_path):
    path = tmp_path / 'image.tar'
    make_image(path, [make_layer({'app/data': b'upstream'})], tamper=True)
    report = audit_image(path)
    assert not report['complete'] and not report['ok']
    assert 'diff_id' in report['error']


@pytest.mark.parametrize('limit', ['max_layer_bytes', 'max_total_bytes', 'max_file_bytes',
                                  'max_members', 'max_archive_bytes'])
def test_audit_stops_at_resource_limits_without_extracting(tmp_path, limit):
    path = tmp_path / 'image.tar'
    make_image(path, [make_layer({'app/one': b'x' * 100000, 'app/two': b'clean'})])
    original = set(tmp_path.iterdir())
    report = audit_image(path, **{limit: 1})
    assert not report['ok'] and not report['complete']
    assert set(tmp_path.iterdir()) == original


def test_configured_secret_across_a_block_boundary_is_found_without_echo():
    secret = 'not-a-standard-key-' + 'q' * 64
    result = scan_bytes(b'\x00' * (1024 * 1024 - 3) + secret.encode(), secret_values=[secret])
    assert result['hits'] == ['configured-secret']
    assert secret not in json.dumps(result)


def test_secret_in_filename_is_redacted(tmp_path):
    path = tmp_path / 'image.tar'
    secret = 'sk-' + 'a' * 48
    make_image(path, [make_layer({'app/' + secret: b'clean'})])
    report = audit_image(path, secret_values=[secret])
    assert report['complete'] and not report['ok']
    assert secret not in json.dumps(report)
    assert any(x.get('path', '').startswith('<redacted-name-') for x in report['findings'])


def test_secret_in_tar_end_padding_is_not_ignored(tmp_path):
    path = tmp_path / 'image.tar'
    secret = b'canary-secret-in-unused-tar-block'
    layer = bytearray(make_layer({'app/source': b'clean'}))
    layer[-100:-100 + len(secret)] = secret
    make_image(path, [bytes(layer)])
    report = audit_image(path, secret_values=[secret])
    assert report['complete'] and not report['ok']
    assert any(x.get('path') == '<unlocated-layer-bytes>' for x in report['findings'])


def test_escaped_json_secret_is_detected(tmp_path):
    path = tmp_path / 'image.tar'
    secret = 'private-' + chr(233) * 20
    make_image(path, [make_layer({'app/source': b'clean'})], history=[{'created_by': secret}])
    report = audit_image(path, secret_values=[secret])
    assert report['complete'] and not report['ok']
    assert any(x['kind'] == 'configured-secret' for x in report['findings'])
    assert secret not in json.dumps(report, ensure_ascii=False)


def test_oversized_pax_metadata_is_refused_before_parsing(tmp_path):
    path = tmp_path / 'image.tar'
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo('app/file')
        member.pax_headers = {'large': 'x' * (1024 * 1024 + 1)}
        archive.addfile(member, io.BytesIO())
    make_image(path, [stream.getvalue()])
    report = audit_image(path)
    assert not report['complete'] and not report['ok']
    assert 'metadata exceeds' in report['error']


def test_expired_deadline_stops_the_audit(tmp_path):
    path = tmp_path / 'image.tar'
    make_image(path, [make_layer({'app/source': b'clean'})])
    report = audit_image(path, timeout=1e-9)
    assert not report['complete'] and not report['ok']
    assert report['error_type'] == 'TimeoutError'


def test_crypto_format_delimiter_is_not_itself_a_private_key():
    result = scan_bytes(b'\x00-----BEGIN RSA PRIVATE KEY-----\n\x00')
    assert result['hits'] == []


@pytest.mark.parametrize('separator', [b'\n', b'\r\n', b'\\n'])
def test_private_key_body_after_a_delimiter_is_detected(separator):
    content = b'-----BEGIN PRIVATE KEY-----' + separator + b'A' * 64 + separator
    result = scan_bytes(b'\x00' * (1024 * 1024 - 10) + content)
    assert result['hits'] == ['credential-like-text']


def public_record(data):
    return ({'sha256': hashlib.sha256(data).hexdigest(),
             'source': 'https://example.invalid/immutable-source',
             'reason': 'public test material'},)


def test_public_material_classification_requires_exact_file_bytes(tmp_path):
    path = tmp_path / 'image.tar'
    data = b'-----BEGIN PRIVATE KEY-----\n' + b'A' * 64 + b'\n'
    make_image(path, [make_layer({'app/example': data})])
    report = audit_image(path, public_material=public_record(data))
    assert report['complete'] and report['ok']
    assert len(report['public_material']) == 1
    make_image(path, [make_layer({'app/example': data + b'changed'})])
    assert not audit_image(path, public_material=public_record(data))['ok']


def test_public_material_does_not_waive_configured_secret_or_internal_identity(tmp_path):
    path = tmp_path / 'image.tar'
    data = b'-----BEGIN PRIVATE KEY-----\n' + b'A' * 64 + b'\n/data/evolvent\n'
    make_image(path, [make_layer({'app/example': data})])
    report = audit_image(path, public_material=public_record(data), secret_values=[b'A' * 40])
    assert report['complete'] and not report['ok']
    assert {x['kind'] for x in report['findings']} == {'configured-secret', 'internal-identity'}


def test_public_key_in_file_does_not_waive_another_key_in_tar_padding(tmp_path):
    path = tmp_path / 'image.tar'
    data = b'-----BEGIN PRIVATE KEY-----\n' + b'A' * 64 + b'\n'
    hidden = b'-----BEGIN PRIVATE KEY-----\n' + b'B' * 64 + b'\n'
    layer = bytearray(make_layer({'app/example': data}))
    layer[-200:-200 + len(hidden)] = hidden
    make_image(path, [bytes(layer)])
    report = audit_image(path, public_material=public_record(data))
    assert report['complete'] and not report['ok']
    assert len(report['public_material']) == 1
    assert any(x['kind'] == 'credential-like-text' and x.get('path') == '<unlocated-layer-bytes>'
               for x in report['findings'])


@pytest.mark.parametrize('leak', [False, True])
@pytest.mark.parametrize('paired,collision', [(False,False), (True,False), (True,True)])
def test_export_commits_only_audited_image_and_keeps_audit_internal(tmp_path, monkeypatch, leak, paired, collision):
    import shutil
    from types import SimpleNamespace
    from frf.core.sandbox import Result
    from frf.observe import images

    source = tmp_path / 'source'
    (source / 'environment').mkdir(parents=True)
    (source / 'environment/Dockerfile').write_text('FROM image\n')
    (source / 'tests').mkdir()
    (source / 'tests/verify.py').write_text('trusted evaluator')
    supplied = tmp_path / 'remote-image.tar'
    make_image(supplied, [make_layer({'app/data': b'/data/evolvent' if leak else b'upstream'})])
    identity = images.archive_metadata(supplied)['image_id']
    expected = images.file_digest(supplied)
    backend = SimpleNamespace(name='remote',
        run=lambda argv, **kw: Result(0, expected if argv[0] == 'sha256sum' else '', ''),
        pull=lambda remote, local: shutil.copyfile(supplied, str(local) + '/image.tar'))
    monkeypatch.setattr(images, 'execution_evidence', lambda *args: SimpleNamespace(ok=True))

    class Replay:
        def __init__(self, backend, log, image_export):
            self.callback = image_export

        def replay(self, path):
            report = {'correctness_passed': 1, 'correctness_total': 1}
            if paired:
                report['verifier_image_id'] = identity
            self.info = self.callback(identity, report)

        def image_check(self, path):
            return {'ok': True, 'image_export': self.info}

    monkeypatch.setattr(images, 'ImageReplay', Replay)
    target = tmp_path / 'delivery/image.tar'
    occupied = target.parent / 'image.verifier.tar.json'
    if collision:
        target.parent.mkdir()
        occupied.write_text('existing user data')
    if leak:
        with pytest.raises(RuntimeError, match='audit did not pass'):
            images.export(source, target, backend=backend)
        assert not target.exists()
        assert not target.with_suffix('.tar.json').exists()
        evidence = list((target.parent / '.frf-evidence').glob('*.failed-audit-*.json'))
        assert len(evidence) == 1
        assert not json.loads(evidence[0].read_text())['ok']
    elif collision:
        with pytest.raises(FileExistsError):
            images.export(source, target, backend=backend)
        assert occupied.read_text() == 'existing user data'
        assert not target.exists()
        assert not (target.parent / 'image.verifier.tar').exists()
        assert not target.with_suffix('.tar.json').exists()
        assert not list((target.parent / '.frf-evidence').glob('*.audit.json'))
    else:
        record = images.export(source, target, backend=backend)
        assert record['archive_sha256'] == expected
        assert 'layer_audit' not in record
        report = json.loads((target.parent / '.frf-evidence/image.tar.audit.json').read_text())
        assert report['complete'] and report['ok']
        assert record['source_environment_sha256'] == images._task_digest(source / 'environment')
        if paired:
            assert (target.parent / 'image.verifier.tar').read_bytes() == supplied.read_bytes()
            receipt = json.loads((target.parent / 'image.verifier.tar.json').read_text())
            assert receipt == record['verifier']
            assert receipt['source_tests_sha256'] == images._task_digest(source / 'tests')
            assert (target.parent / '.frf-evidence/image.verifier.tar.audit.json').is_file()


def test_public_ssh_algorithm_name_is_not_an_api_key_exemption_for_its_neighbors():
    name = b'sk-ecdsa-sha2-nistp256-cert-v01@openssh.com'
    assert scan_bytes(name)['hits'] == []
    assert scan_bytes(name.split(b'@')[0])['hits'] == ['credential-like-text']
    assert scan_bytes(name + b'\x00sk-' + b'x' * 40)['hits'] == ['credential-like-text']
    assert scan_bytes(name, secret_values=[name])['hits'] == ['configured-secret']
