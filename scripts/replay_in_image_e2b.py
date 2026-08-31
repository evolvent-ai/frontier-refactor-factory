#!/usr/bin/env python3
"""Replay an emitted task's reference inside the image the task actually ships.

WHAT THIS CLOSES. `evidence.package_reproduces_itself` (E7) already drives the emitted package with
the reference it ships and requires 100%. But `checkout_task.drive` runs `python3 tests/verify.py`
as a HOST subprocess, so what E7 proves is that the build machine reproduces expectations frozen in
an E2B sandbox. Three environments are involved and none of them is the one the task delivers:

    expectations frozen in .......... an E2B sandbox
    E7 replays in ................... this host
    the task ships .................. environment/Dockerfile, never built during the pipeline

A reviewed delivery elsewhere failed exactly on that gap: its answer key was generated where `tree`
was absent, the delivered image had `tree`, and a behaviourally perfect reference scored 86.4%
instead of 100%. Nothing in the pipeline would have caught it, because nothing ran the verifier
where the task runs.

WHY THIS IS A SEPARATE TOOL AND NOT A PIPELINE STAGE, for now. Building a task image is minutes and
gigabytes -- the rust base alone is ~2GB -- so making it inline multiplies batch wall time by an
amount nobody has measured. This measures it. If the per-task cost turns out small, it belongs in
`emit`; if it is large, it belongs in a nightly sweep over the corpus. Either way the code is the
same, and a task that fails here must lose its attestation rather than stay in the pool.

ALL OF IT RUNS IN E2B, including the docker build, using the DinD template the rest of the factory
uses. Nothing is built on this host: an image built here would prove something about here.

    .venv/bin/python scripts/replay_in_image_e2b.py <task-dir> [<task-dir> ...]
    .venv/bin/python scripts/replay_in_image_e2b.py --root <results-dir> [--concurrent 4]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import credentials                                       # noqa: E402

# Long enough for a cold `docker build` of a compiled-language base, bounded so one task cannot hold
# a sweep. The same two-timeout rule the rest of the factory learned applies: every SDK call below
# passes `request_timeout`, because an unbounded remote call does not fail, it waits.
BUILD_TIMEOUT = 1800
REPLAY_TIMEOUT = 1800
OPEN_TIMEOUT = 120
TRANSFER_TIMEOUT = 600

# Excluded from the upload for the same reason the sandbox push excludes them: they are large, they
# are rebuilt inside the image anyway, and shipping them changes what is being tested.
_SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules", "target", ".venv"}


# How many times a build is retried when what failed was the wire.
BUILD_ATTEMPTS = 3

# What a transient build failure looks like. Named rather than "any failure retried", so a Dockerfile
# that is genuinely wrong still fails on the first try and says so.
_TRANSPORT = (
    "tls: bad record MAC", "TLS handshake timeout", "connection reset by peer",
    "Temporary failure resolving", "Could not connect to", "Connection timed out",
    "i/o timeout", "unexpected EOF", "500 Internal Server Error", "503 Service Unavailable",
    "net/http: TLS handshake", "failed to copy: httpReadSeeker",
)


def replay_one(task_dir: str, api_key: str, template: str) -> dict:
    """Build this task's own image in E2B and run its verifier inside it. -> a result record.

    ONE IMPLEMENTATION, SHARED WITH THE GATE. This began here, as a tool run after a corpus was
    finished -- which is to say, a tool whose findings arrive too late. `frf.observe.in_image` is
    now the same code the pipeline runs before it attests anything, so what this audits is exactly
    what that enforces. Two copies would drift, and the drift would be invisible in precisely the
    way both of them exist to prevent.
    """
    from frf.observe import in_image

    return in_image.drive(task_dir, api_key=api_key, template=template)


def _task_dirs(root: str) -> list[str]:
    return sorted({os.path.dirname(p) for p in
                   (os.path.join(d, f) for d, _s, fs in os.walk(root) for f in fs
                    if f == "task.toml")})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tasks", nargs="*", help="task directories")
    parser.add_argument("--root", help="a results directory; every task under it is replayed")
    parser.add_argument("--concurrent", type=int, default=3)
    parser.add_argument("--json", help="write the full report here")
    args = parser.parse_args()

    targets = list(args.tasks)
    if args.root:
        targets += _task_dirs(args.root)
    if not targets:
        print("nothing to replay: pass task directories or --root", file=sys.stderr)
        return 2

    api_key = credentials.get("E2B_API_KEY")
    template = credentials.get("E2B_DIND_TEMPLATE") or credentials.get("E2B_TEMPLATE") or ""
    if not api_key:
        print("E2B_API_KEY is not set; this tool builds only in E2B by design", file=sys.stderr)
        return 2

    print("replaying %d task(s) inside their own images, %d at a time"
          % (len(targets), args.concurrent), flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrent)) as pool:
        futures = {pool.submit(replay_one, t, api_key, template): t for t in targets}
        for future in as_completed(futures):
            record = future.result()
            results.append(record)
            mark = "ok  " if record["ok"] else "FAIL"
            print("  %s %-42s %6.0fs  %s" % (mark, record["task"][:42], record["seconds"],
                                             record["detail"][:90]), flush=True)

    passed = sum(1 for r in results if r["ok"])
    seconds = [r["seconds"] for r in results]
    print("\n%d/%d reproduce inside their delivered image" % (passed, len(results)))
    if seconds:
        print("per-task cost: median %.0fs, max %.0fs -- this is what an inline gate would add"
              % (sorted(seconds)[len(seconds) // 2], max(seconds)))
    failures = [r for r in results if not r["ok"]]
    if failures:
        print("\nthese must lose their attestation rather than stay in the pool:")
        for record in failures:
            print("  %s (%s) %s" % (record["task"], record["stage"] or "replay",
                                    record["detail"][-160:]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=1)
        print("\nreport: %s" % args.json)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
