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


def _tar_bytes(root: str) -> bytes:
    """The task directory as a tar stream, deterministically ordered."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP)
            for name in sorted(files):
                full = os.path.join(directory, name)
                archive.add(full, arcname=os.path.relpath(full, root))
    return buffer.getvalue()


def replay_one(task_dir: str, api_key: str, template: str) -> dict:
    """Build this task's own image in E2B and run its verifier inside it. -> a result record."""
    from e2b import Sandbox

    name = os.path.basename(task_dir.rstrip("/"))
    started = time.monotonic()
    record = {"task": name, "path": task_dir, "ok": False, "stage": "", "detail": "",
              "seconds": 0.0}

    dockerfile = os.path.join(task_dir, "environment", "Dockerfile")
    if not os.path.isfile(dockerfile):
        record.update(stage="dockerfile", detail="no environment/Dockerfile to build")
        return record

    sandbox = None
    try:
        sandbox = Sandbox.create(template=template, timeout=BUILD_TIMEOUT + REPLAY_TIMEOUT,
                                 api_key=api_key, request_timeout=OPEN_TIMEOUT)
        remote = "/tmp/frf-replay-%d" % (abs(hash(task_dir)) % 10 ** 10)
        sandbox.commands.run("mkdir -p %s" % remote, timeout=30, request_timeout=60)
        sandbox.files.write("%s/task.tar" % remote, _tar_bytes(task_dir),
                            request_timeout=TRANSFER_TIMEOUT)
        sandbox.commands.run("tar -xf %s/task.tar -C %s" % (remote, remote),
                             timeout=120, request_timeout=180)

        scale = ""
        try:
            with open(os.path.join(task_dir, "task.toml"), encoding="utf-8") as handle:
                for line in handle:
                    if line.strip().startswith("scale = "):
                        scale = line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            pass
        is_repo = scale == "repo"
        record["scale"] = scale

        tag = "frf-replay-%s" % name.lower().replace("@", "-")[:40]
        record["stage"] = "build"
        built = sandbox.commands.run(
            "docker build --pull -t %s %s/environment" % (tag, remote),
            timeout=BUILD_TIMEOUT, request_timeout=BUILD_TIMEOUT + 120)
        if built.exit_code != 0:
            record["detail"] = ((built.stdout or "") + (built.stderr or ""))[-700:]
            return record

        # THE VERIFIER, INSIDE THE IMAGE, against the reference the task ships -- driven exactly as
        # the host-side E7 drives it, because a difference in HOW it is driven would show up as an
        # environment difference and this tool would be lying about which one it found.
        #
        # THE TWO SEAMS ARE DRIVEN DIFFERENTLY, and assuming otherwise is how the first run of this
        # tool "failed" a task that was fine: the repo scale takes `--self-replay` and writes
        # REWARD_PATH, while the call seam takes a workspace pointing at the shipped reference and
        # reports on STDOUT. Read `observe/call/package.drive` and `observe/checkout_task.drive`
        # together before changing either of these.
        record["stage"] = "replay"
        if is_repo:
            command = ("REWARD_PATH=/tmp/reward.json python3 tests/verify.py "
                       "--task-root tests --workspace environment --self-replay; "
                       "cat /tmp/reward.json 2>/dev/null")
        else:
            command = ("REWARD_PATH=/tmp/reward.json SUBMISSION_ROOT=tests/reference "
                       "python3 tests/verify.py --task-root tests --workspace tests/reference; "
                       "cat /tmp/reward.json 2>/dev/null")
        replay = sandbox.commands.run(
            "docker run --rm -v %s:/task -w /task %s sh -c %s"
            % (remote, tag, json.dumps(command)),
            timeout=REPLAY_TIMEOUT, request_timeout=REPLAY_TIMEOUT + 120)
        # Either seam may report on stdout or in the file; take whichever carries a graded total,
        # and keep stderr for the message when neither does.
        blob = (replay.stdout or "") + "\n" + (replay.stderr or "")
        # SCANNED AS OBJECTS, NOT AS LINES. The report is pretty-printed, so a line-oriented parse
        # sees `{` alone and finds nothing -- which reads as "the verifier said nothing" when it in
        # fact said 57/57. Walk the blob and decode each balanced object.
        report = None
        decoder = json.JSONDecoder()
        position = 0
        while True:
            start = blob.find("{", position)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(blob, start)
            except ValueError:
                position = start + 1
                continue
            position = end
            if isinstance(value, dict) and "correctness_total" in value:
                report = value
        if report is None:
            record["detail"] = "verifier produced no graded report: " + blob.strip()[-700:]
            return record

        passed = int(report.get("correctness_passed", 0))
        total = int(report.get("correctness_total", 0))
        record.update(passed=passed, total=total)
        if total <= 0:
            record["detail"] = "the shipped verifier graded nothing inside the image"
        elif passed == total:
            record.update(ok=True, stage="", detail="%d/%d inside the delivered image"
                                                    % (passed, total))
        else:
            record["detail"] = ("%d/%d inside the delivered image -- the expectations were frozen "
                                "somewhere this image does not reproduce" % (passed, total))
        return record
    except Exception as exc:                                   # noqa: BLE001 -- reported, not raised
        record["detail"] = "%s: %s" % (type(exc).__name__, str(exc)[:400])
        return record
    finally:
        record["seconds"] = round(time.monotonic() - started, 1)
        if sandbox is not None:
            try:
                sandbox.kill(request_timeout=30)
            except Exception:                                  # noqa: BLE001 -- teardown
                pass


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
                                    record["detail"][:120]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=1)
        print("\nreport: %s" % args.json)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
