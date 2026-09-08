#!/usr/bin/env python3
"""Audit a task/release tree against the anonymous public delivery contract.

This is intentionally a report/gate, not a Docker builder. Internal E2B production may use
networked provisioning; a public release must either ship a prepared immutable image reference or
have a Dockerfile that can build without network access. Upstream project identities remain allowed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frf.core.public_scan import scan_stream

DOCKER_NETWORK = re.compile(r"\b(apt-get|apt|pip install|npm install|yarn install|pnpm install|cargo install|git clone|curl |wget )\b", re.I)
FROM = re.compile(r"^\s*FROM\s+([^\s]+)", re.I | re.M)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def audit(root: str) -> dict:
    base = Path(root)
    findings = []
    if not base.is_dir():
        return {"ok": False, "findings": [{"severity": "error", "kind": "missing-directory"}]}
    files = [p for p in base.rglob("*") if p.is_file()]
    image_records = list(base.rglob('*.tar.json')) + list(base.rglob('image.json'))
    verified_archives = []
    verified_tags = set()
    for record_path in image_records:
        relative = record_path.relative_to(base).as_posix()
        try:
            if record_path.is_symlink():
                raise ValueError('linked receipt')
            record = json.loads(record_path.read_text())
            name = record.get('archive')
            if not isinstance(name, str) or not name or Path(name).name != name:
                raise ValueError('invalid archive name')
            archive_path = record_path.parent / name
            if archive_path.is_symlink() or not archive_path.resolve().is_relative_to(base.resolve()):
                raise ValueError('archive escapes release directory')
            if file_digest(archive_path) != record.get('archive_sha256'):
                findings.append({'severity': 'error', 'kind': 'image-archive-digest-mismatch', 'path': relative})
                continue
            verified_archives.append(archive_path.resolve().relative_to(base.resolve()).as_posix())
            # A valid checksum alone cannot authorize a base-image tag.
            from frf.observe.images import archive_metadata
            from frf.observe.image_audit import BoundedTarInfo
            actual = archive_metadata(archive_path, tarinfo=BoundedTarInfo)
            if any(record.get(key) != actual[key] for key in ('image_id', 'image_tag', 'config_id')):
                raise ValueError('receipt does not identify its archive image')
            verified_tags.add(actual['image_tag'])
        except (OSError, TypeError, ValueError, KeyError, tarfile.TarError):
            findings.append({'severity': 'error', 'kind': 'invalid-image-record', 'path': relative})
    for path in files:
        relative = path.relative_to(base).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(base.resolve()):
                findings.append({"severity": "error", "kind": "external-symlink", "path": relative})
            continue
        try:
            with path.open('rb') as handle:
                hits = scan_stream(handle)['hits']
            text = path.read_text(errors='replace') if path.name == 'Dockerfile' else ''
        except OSError:
            findings.append({"severity": "error", "kind": "unreadable-file", "path": relative})
            continue
        findings.extend({"severity": "error", "kind": kind, "path": relative} for kind in sorted(hits))
        if path.name == "Dockerfile":
            for image in FROM.findall(text):
                if not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image) and image not in verified_tags:
                    findings.append({"severity": "error", "kind": "unpinned-base-image",
                                     "path": relative, "image": image})
            if DOCKER_NETWORK.search(text):
                findings.append({"severity": "error", "kind": "networked-build-step", "path": relative})
        if path.name == '.env' or (path.name.startswith('.env.') and path.name != '.env.example'):
            findings.append({"severity": "error", "kind": "dotenv-in-release", "path": relative})
    # A checksum proves byte integrity, not a valid image, anonymity of its layers, or an
    # offline rebuild. It must never waive findings for unrelated Dockerfiles in a release.
    return {"files": len(files), "findings": findings,
            "ok": not any(x["severity"] == "error" for x in findings),
            "verified_archive_checksums": verified_archives, "release_ready": False,
            "verified_archive_tags": sorted(verified_tags),
            "runtime_required": ["archive-load", "offline-build", "offline-run", "solver-isolation"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--json")
    args = parser.parse_args()
    report = audit(args.root)
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
