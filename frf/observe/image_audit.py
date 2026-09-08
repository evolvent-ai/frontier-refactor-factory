"""Inspect every Docker image layer without extracting or executing its contents."""
from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
import time
from bisect import bisect_right
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from ..core import resources
from ..core import public_scan
from ..core.public_scan import ContentScanner, scan_bytes, scan_stream
from ..core.public_material import PUBLIC_MATERIAL
from .images import archive_metadata


class AuditFailure(ValueError):
    """An audit failure whose message contains no untrusted archive content."""


class BoundedTarInfo(tarfile.TarInfo):
    def _proc_pax(self, archive):
        if self.size > 1024 * 1024:
            raise AuditFailure('tar extended metadata exceeds audit limit')
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size > 1024 * 1024:
            raise AuditFailure('tar long name exceeds audit limit')
        return super()._proc_gnulong(archive)

    def _proc_sparse(self, archive):
        raise AuditFailure('sparse image members require separate review')


class LayerReader:
    def __init__(self, handle, budget, layer_limit, deadline, secret_values=(), *, scan=False):
        self.handle, self.budget = handle, budget
        self.layer_limit, self.deadline = layer_limit, deadline
        self.size = 0
        self.scanner = ContentScanner(secret_values, track_matches=True) if scan else None
        self.digest = self.scanner.digest if self.scanner else hashlib.sha256()

    def read(self, size):
        if size < 0 or size > 1024 * 1024:
            raise AuditFailure('unbounded image-layer read refused')
        if time.monotonic() > self.deadline:
            raise TimeoutError('image audit deadline exceeded')
        block = self.handle.read(size)
        self.size += len(block)
        self.budget['bytes'] += len(block)
        if self.size > self.layer_limit or self.budget['bytes'] > self.budget['limit']:
            raise AuditFailure('image decompression byte limit exceeded')
        if self.scanner:
            self.scanner.update(block)
        else:
            self.digest.update(block)
        return block


def audit_image(path, *, secret_values=(), max_layer_bytes=8 * resources.GiB,
                max_total_bytes=32 * resources.GiB, max_file_bytes=2 * resources.GiB,
                max_archive_bytes=16 * resources.GiB, max_members=250000, timeout=300,
                public_material=PUBLIC_MATERIAL, log=lambda _message: None):
    """Return findings with content hashes, never matched secret values.

    All historical layers are scanned, including files hidden by later whiteouts.
    Embedded arbitrary file formats are scanned as bytes, not recursively unpacked.
    """
    if min(max_layer_bytes, max_total_bytes, max_file_bytes, max_archive_bytes, max_members, timeout) <= 0:
        raise ValueError('image audit limits must be positive')
    path = Path(path)
    resources.require_headroom(path.parent, transfer_bytes=8 * 1024 * 1024)
    deadline = time.monotonic() + timeout
    budget = {'bytes': 0, 'limit': max_total_bytes}
    classifications = {item['sha256']: item for item in public_material}
    report = {'findings': [], 'layers': [], 'public_material': [],
              'complete': False, 'ok': False, 'members': 0, 'release_ready': False,
              'scope': 'archive metadata, image config/history, all layer headers and file bytes',
              'limitations': ['embedded file archives are not recursively unpacked']}
    implementation = (Path(__file__).read_bytes() + Path(public_scan.__file__).read_bytes()
                      + json.dumps(public_material, sort_keys=True).encode())
    report['audit_implementation_sha256'] = hashlib.sha256(implementation).hexdigest()

    def record_scan(data, location):
        location = dict(location)
        for key in ('path', 'archive_member'):
            if key in location:
                name = scan_bytes(location[key].encode(), secret_values=secret_values)
                if set(name['hits']) & {'credential-like-text', 'configured-secret'}:
                    location[key] = '<redacted-name-sha256:' + name['sha256'] + '>'
        for kind in data['hits']:
            finding = dict(kind=kind, **location, sha256=data['sha256'])
            classification = classifications.get(data['sha256'])
            if kind == 'credential-like-text' and classification:
                report['public_material'].append(dict(finding, classification=classification))
            else:
                report['findings'].append(finding)
        if len(report['findings']) > 10000:
            raise AuditFailure('image finding count limit exceeded')
        return set(data['hits'])

    try:
        if path.stat().st_size > max_archive_bytes:
            raise AuditFailure('image archive exceeds audit byte limit')
        with path.open('rb') as handle:
            raw = LayerReader(handle, {'bytes': 0, 'limit': max_archive_bytes}, max_archive_bytes, deadline)
            while raw.read(1024 * 1024):
                pass
            report['archive_sha256'] = raw.digest.hexdigest()
        # Exported Docker archives are uncompressed tar containers; their individual
        # layers may be compressed. Refuse outer compression to bound header parsing.
        with tarfile.open(path, 'r:', tarinfo=BoundedTarInfo):
            pass
        report['image'] = archive_metadata(path, tarinfo=BoundedTarInfo)
        with tarfile.open(path, 'r:', tarinfo=BoundedTarInfo) as outer:
            def read_json(name):
                member = outer.getmember(name)
                if not member.isfile() or member.size > 4 * 1024 * 1024:
                    raise AuditFailure('invalid image JSON member')
                with outer.extractfile(member) as handle:
                    return json.load(handle)

            manifest = read_json('manifest.json')[0]
            config = read_json(manifest['Config'])
            layer_names = manifest.get('Layers') or []
            # Include unused archive metadata: a correct image graph does not make extra
            # author fields or secret-bearing build records safe to distribute.
            for member in outer.getmembers():
                location = {'archive_member': member.name}
                header = json.dumps({'name': member.name, 'uname': member.uname,
                                     'gname': member.gname, 'pax': member.pax_headers}).encode()
                record_scan(scan_bytes(header, secret_values=secret_values), location)
                if member.isfile() and member.name not in layer_names:
                    if member.size > min(max_file_bytes, 4 * 1024 * 1024):
                        raise AuditFailure('undeclared image payload or oversized metadata')
                    with outer.extractfile(member) as handle:
                        metadata_bytes = handle.read()
                    record_scan(scan_bytes(metadata_bytes, secret_values=secret_values), location)
                    decoded_metadata = json.loads(metadata_bytes)
                    record_scan(scan_bytes(json.dumps(decoded_metadata, ensure_ascii=False).encode(),
                                           secret_values=secret_values), location)
            for index, name in enumerate(layer_names):
                log('image audit: layer %d/%d' % (index + 1, len(layer_names)))
                with ExitStack() as stack:
                    compressed = stack.enter_context(outer.extractfile(name))
                    signature = compressed.read(4)
                    compressed.seek(0)
                    if signature.startswith(b'\x1f\x8b'):
                        decoded = stack.enter_context(gzip.GzipFile(fileobj=compressed))
                    elif signature == b'\x28\xb5\x2f\xfd':
                        raise AuditFailure('zstd image layers require separate review')
                    else:
                        decoded = compressed
                    reader = LayerReader(decoded, budget, max_layer_bytes, deadline, secret_values, scan=True)
                    layer = stack.enter_context(tarfile.open(fileobj=reader, mode='r|',
                                                            tarinfo=BoundedTarInfo))
                    count = 0
                    file_ranges = []
                    for member in layer:
                        report['members'] += 1
                        count += 1
                        if report['members'] > max_members:
                            raise AuditFailure('image member count limit exceeded')
                        name_path = PurePosixPath(member.name)
                        if name_path.is_absolute() or '..' in name_path.parts or member.sparse:
                            raise AuditFailure('invalid or sparse image-layer member')
                        location = {'layer': index, 'path': member.name}
                        header = json.dumps({'name': member.name, 'linkname': member.linkname,
                                             'uname': member.uname, 'gname': member.gname,
                                             'pax': member.pax_headers}).encode()
                        record_scan(scan_bytes(header, secret_values=secret_values), location)
                        if member.isfile():
                            if member.size > max_file_bytes:
                                raise AuditFailure('image file exceeds audit byte limit')
                            with layer.extractfile(member) as handle:
                                hits = record_scan(scan_stream(handle, secret_values=secret_values,
                                                               max_bytes=max_file_bytes), location)
                            file_ranges.append((member.offset_data, member.offset_data + member.size, hits))
                        layer.members.clear()
                    while reader.read(1024 * 1024):
                        pass
                    raw_scan = reader.scanner.result()
                    starts = [start for start, _end, _hits in file_ranges]
                    for match in raw_scan['matches']:
                        slot = bisect_right(starts, match['start']) - 1
                        if slot >= 0:
                            _start, end, hits = file_ranges[slot]
                            if match['end'] <= end and match['kind'] in hits:
                                continue
                        unlocated = dict(raw_scan, hits=[match['kind']])
                        record_scan(unlocated, {'layer': index, 'path': '<unlocated-layer-bytes>',
                                                'offset': match['start']})
                    diff_id = 'sha256:' + reader.digest.hexdigest()
                    if diff_id != config['rootfs']['diff_ids'][index]:
                        raise AuditFailure('image layer content does not match its diff_id')
                    compressed.seek(0)
                    compressed_digest = hashlib.sha256()
                    for block in iter(lambda: compressed.read(1024 * 1024), b''):
                        compressed_digest.update(block)
                    if name.startswith('blobs/sha256/') and name.rsplit('/', 1)[1] != compressed_digest.hexdigest():
                        raise AuditFailure('image blob content does not match its digest')
                    report['layers'].append({'index': index, 'diff_id': diff_id, 'members': count,
                                             'uncompressed_bytes': reader.size})
        report['complete'] = True
        report['ok'] = not report['findings']
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError) as error:
        # Error text can include an attacker-controlled archive filename; keep it internal.
        report['error_type'] = type(error).__name__
        report['error'] = str(error) if isinstance(error, AuditFailure) else 'image structure or read failed'
    report['uncompressed_bytes'] = budget['bytes']
    return report
