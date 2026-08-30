#!/usr/bin/env python3
"""Collect emitted tasks into one release directory, with a manifest and checksums.

WHY THIS EXISTS. Tasks land under `<output>/.candidates/<hash>/<name>/`, which is a production
layout: the hash keeps concurrent candidates from colliding and means nothing to anyone reading the
corpus. A release is a flat, named set with something that says what is in it and whether it arrived
intact. Reviewed deliveries are rejected for the absence of exactly that -- a checksum list whose
entries no longer match, or a manifest naming files that are not there.

WHAT IT REFUSES TO INCLUDE, and this is the point rather than a convenience:

    a task with no attestation record          nothing established that it works
    a task whose decisive checks did not hold  ceiling, floor, package-reproduces-itself
    a task that failed the in-image replay     it does not reproduce where it is delivered

The last one is read from `replay_in_image_e2b.py --json`. A task that fails there must lose its
place in the pool rather than be shipped with a note, which is the whole reason that tool prints
"these must lose their attestation".

DEDUPLICATION IS BY SUBJECT, not by directory. Roll mode nests each candidate under its own hash and
a subject re-frozen with a different probe count leaves a second directory behind; both counts are
printed, because the gap between them is worth seeing.

    .venv/bin/python scripts/package_release.py <results-dir> --into <release-dir> \
        [--replay-report replay.json] [--tar]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import audit                                            # noqa: E402


def _digest(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def _tree_digest(root: str) -> tuple[str, int, int]:
    """One digest over a whole task. -> (digest, file count, bytes).

    Over the RELATIVE PATH as well as the content, so moving a file inside the task changes the
    digest. A digest of concatenated contents alone would call two different layouts identical.
    """
    sha = hashlib.sha256()
    files = size = 0
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(dirs)
        for name in sorted(names):
            full = os.path.join(directory, name)
            relative = os.path.relpath(full, root)
            sha.update(relative.encode("utf-8"))
            sha.update(_digest(full).encode("ascii"))
            files += 1
            size += os.path.getsize(full)
    return sha.hexdigest(), files, size


def _metadata(task_dir: str) -> dict:
    """The few task.toml fields a reader of the manifest actually wants."""
    wanted = ("name", "scale", "source_language", "target_language", "cross_language",
              "evidence_checks_held", "evidence_checks_total", "evidence_backend")
    found: dict = {}
    try:
        with open(os.path.join(task_dir, "task.toml"), encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition("=")
                key = key.strip()
                if key in wanted and key not in found:
                    found[key] = value.strip().strip('"')
    except OSError:
        pass
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", help="the directory a run wrote into")
    parser.add_argument("--into", required=True, help="where the release is assembled")
    parser.add_argument("--replay-report", help="JSON from replay_in_image_e2b.py")
    parser.add_argument("--tar", action="store_true", help="also write <into>.tar.gz")
    parser.add_argument("--allow-unreplayed", action="store_true",
                        help="include tasks with no in-image replay result (records them as such)")
    args = parser.parse_args()

    subjects = audit.walk(args.results)
    attested = [s for s in subjects if s.status == audit.ATTESTED]

    replayed: dict = {}
    if args.replay_report:
        with open(args.replay_report, encoding="utf-8") as handle:
            for record in json.load(handle):
                replayed[os.path.basename(str(record.get("path", "")).rstrip("/"))] = record

    os.makedirs(args.into, exist_ok=True)
    entries = []
    skipped = []
    seen_names: dict = {}
    for subject in attested:
        # `paths` rather than `path`: one subject can have several task directories when it was
        # re-frozen, and the first is the one shipped -- the rest are recorded as duplicates below.
        if not subject.paths:
            skipped.append((subject.identity, "attested but no directory on disk"))
            continue
        source = subject.paths[0]
        name = os.path.basename(source.rstrip("/"))
        replay = replayed.get(name)
        if replay is not None and not replay.get("ok"):
            skipped.append((name, "failed in-image replay: %s" % str(replay.get("detail"))[:120]))
            continue
        if replay is None and args.replay_report and not args.allow_unreplayed:
            skipped.append((name, "no in-image replay result"))
            continue
        # A subject re-frozen twice leaves two directories with one name; keep the first and say so
        # rather than overwriting, which would make the count and the contents disagree.
        if name in seen_names:
            skipped.append((name, "duplicate of %s" % seen_names[name]))
            continue
        seen_names[name] = source

        destination = os.path.join(args.into, name)
        shutil.copytree(source, destination, dirs_exist_ok=True)
        digest, files, size = _tree_digest(destination)
        entries.append({"name": name, "tree_sha256": digest, "files": files, "bytes": size,
                        "in_image_replay": (replay or {}).get("detail", "not run"),
                        **_metadata(destination)})

    entries.sort(key=lambda e: (e.get("scale", ""), e["name"]))
    scales: dict = {}
    languages: dict = {}
    for entry in entries:
        scales[entry.get("scale", "?")] = scales.get(entry.get("scale", "?"), 0) + 1
        languages[entry.get("source_language", "?")] = \
            languages.get(entry.get("source_language", "?"), 0) + 1

    manifest = {"tasks": len(entries), "by_scale": scales, "by_source_language": languages,
                "entries": entries}
    with open(os.path.join(args.into, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)

    # A flat checksum list over every shipped file, so `sha256sum -c` is the whole integrity check.
    lines = []
    for directory, dirs, names in os.walk(args.into):
        dirs[:] = sorted(dirs)
        for name in sorted(names):
            if name == "checksums.sha256":
                continue
            full = os.path.join(directory, name)
            lines.append("%s  %s" % (_digest(full), os.path.relpath(full, args.into)))
    with open(os.path.join(args.into, "checksums.sha256"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    print("release: %s" % args.into)
    print("  %d task(s) from %d attested subject(s)" % (len(entries), len(attested)))
    print("  by scale    : %s" % json.dumps(scales, sort_keys=True))
    print("  by language : %s" % json.dumps(languages, sort_keys=True))
    print("  checksums   : %d file(s)" % len(lines))
    if skipped:
        print("  held back   : %d" % len(skipped))
        for name, why in skipped[:12]:
            print("      %-40s %s" % (name[:40], why))

    if args.tar:
        archive = args.into.rstrip("/") + ".tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(args.into, arcname=os.path.basename(args.into.rstrip("/")))
        print("  archive     : %s (%.1f MB)" % (archive, os.path.getsize(archive) / 1e6))

    # Verified here rather than trusted: a checksum list that does not check is worse than none,
    # because it reads as evidence.
    check = subprocess.run(["sha256sum", "-c", "checksums.sha256", "--quiet"],
                           cwd=args.into, capture_output=True, text=True)
    print("  integrity   : %s" % ("verified" if check.returncode == 0
                                  else "FAILED\n" + check.stdout[-400:]))
    return 0 if check.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
