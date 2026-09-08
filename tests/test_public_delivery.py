import json
from pathlib import Path

from scripts.audit_public_delivery import audit


def test_public_audit_allows_upstream_identity_but_rejects_ours(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text(
        "FROM python:3.12@sha256:" + "a" * 64 + "\nCOPY . /app\n")
    (tmp_path / "README.md").write_text("Source: github.com/wasmtime/wasmtime\n")
    assert audit(str(tmp_path))["ok"]
    (tmp_path / "README.md").write_text("internal /data/evolvent/shijian-workdir\n")
    report = audit(str(tmp_path))
    assert not report["ok"] and any(x["kind"] == "internal-identity" for x in report["findings"])


def test_public_audit_rejects_unreproducible_build_steps(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\nRUN apt-get update && pip install x\n")
    report = audit(str(tmp_path))
    kinds = {x["kind"] for x in report["findings"]}
    assert {"unpinned-base-image", "networked-build-step"} <= kinds


def test_binary_files_cannot_hide_internal_identity(tmp_path):
    (tmp_path / "reference.bin").write_bytes(b"\x00e2b_deadbeef_token /data/evolvent\x00")
    report = audit(str(tmp_path))
    assert not report["ok"]
    assert any(x['kind'] == 'internal-identity' for x in report['findings'])


def test_fake_archive_record_cannot_read_outside_release(tmp_path):
    import json
    (tmp_path / 'image.json').write_text(json.dumps({'archive': '/etc/passwd', 'archive_sha256': 'abc'}))
    report = audit(str(tmp_path))
    assert any(x['kind'] == 'invalid-image-record' for x in report['findings'])


def test_ordinary_upstream_token_variable_is_not_a_credential(tmp_path):
    (tmp_path / 'parser.py').write_text('token = scanner.next()\n')
    assert audit(str(tmp_path))['ok']


def test_mask_identifier_in_binary_is_not_an_api_key(tmp_path):
    (tmp_path / 'program').write_bytes(b'\x00mask-' + b'a' * 196 + b'\x00')
    assert audit(str(tmp_path))['ok']


def test_api_key_crossing_a_read_boundary_is_detected(tmp_path):
    payload = b'\x00' * (1024 * 1024 - 2) + b'sk-' + b'a' * 40 + b'\x00'
    (tmp_path / 'program').write_bytes(payload)
    report = audit(str(tmp_path))
    assert not report['ok']
    assert any(item['kind'] == 'credential-like-text' for item in report['findings'])


def test_api_key_after_a_mask_label_is_still_detected(tmp_path):
    (tmp_path / 'program').write_bytes(b'mask-sk-' + b'a' * 40)
    assert not audit(str(tmp_path))['ok']


def test_content_tag_requires_the_matching_verified_image_archive(tmp_path):
    import json
    from tests.test_image_audit import make_image, make_layer
    from frf.observe.images import archive_metadata, file_digest
    archive = tmp_path / 'runtime.tar'
    make_image(archive, [make_layer({'app/data': b'public data'})])
    metadata = archive_metadata(archive)
    (tmp_path / 'Dockerfile').write_text('FROM ' + metadata['image_tag'] + '\n')
    assert not audit(tmp_path)['ok']
    metadata.update(archive=archive.name, archive_sha256=file_digest(archive))
    receipt = tmp_path / 'runtime.tar.json'
    receipt.write_text(json.dumps(metadata))
    result = audit(tmp_path)
    assert result['ok'], result
    assert result['verified_archive_tags'] == [metadata['image_tag']]
    metadata['image_id'] = 'sha256:' + '0' * 64
    receipt.write_text(json.dumps(metadata))
    result = audit(tmp_path)
    assert not result['ok']
    assert not result['verified_archive_tags']


def test_digest_marker_alone_cannot_pass_as_a_pinned_base(tmp_path):
    (tmp_path / 'Dockerfile').write_text('FROM runtime@sha256:invalid\n')
    assert any(item['kind'] == 'unpinned-base-image' for item in audit(tmp_path)['findings'])


def test_assembled_release_accepts_task_local_tags_only_with_verified_bundle(tmp_path):
    import shutil
    from tests.test_image_audit import make_image, make_layer
    from frf.observe.images import archive_metadata, file_digest
    task = tmp_path / 'task'
    (task / 'environment').mkdir(parents=True)
    (task / 'tests').mkdir()
    solver = tmp_path / 'solver.tar'
    verifier = tmp_path / 'solver.verifier.tar'
    make_image(solver, [make_layer({'app/data': b'solver'})])
    make_image(verifier, [make_layer({'tests/verify.py': b'verifier'})])
    sm = archive_metadata(solver)
    vm = archive_metadata(verifier)
    (task / 'environment/Dockerfile').write_text('FROM %s\n' % sm['image_tag'])
    (task / 'tests/Dockerfile').write_text('FROM %s\n' % vm['image_tag'])
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    shutil.copyfile(solver, bundle / solver.name)
    shutil.copyfile(verifier, bundle / verifier.name)
    sm.update(archive=solver.name, archive_sha256=file_digest(solver))
    vm.update(archive=verifier.name, archive_sha256=file_digest(verifier))
    (bundle / 'solver.tar.json').write_text(json.dumps(sm))
    (bundle / 'solver.verifier.tar.json').write_text(json.dumps(vm))
    release = tmp_path / 'release'
    shutil.copytree(task, release / 'task')
    shutil.copytree(bundle, release / 'images')
    assert audit(release)['ok']
    (release / 'images/solver.tar').write_bytes(b'tampered')
    assert not audit(release)['ok']
