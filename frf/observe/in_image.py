"""Build a task's own image and run its shipped verifier inside it.

THE GAP THIS CLOSES. Every other gate measures the container the task was PRODUCED in. The task
ships a Dockerfile, and until this existed nothing had ever executed it -- so a task could pass the
freeze, the adequacy audit, the whole evidence battery and `package-reproduces-itself`, and still
describe an environment its recipient will never have.

That is not a theoretical gap. In one finished corpus about forty JavaScript and TypeScript tasks
shipped a Dockerfile that could not be built at all: the node base image already carries a yarn and
npm 9 refuses to overwrite it, so `npm install -g yarn` exited 1 and the image never existed. All of
them were attested. Beyond that, twenty-five package tasks built and then reproduced an average of
18% of their own graded probes, because the production container resolved dependencies at one moment
and the delivered image resolves them again at another.

WHY IT LIVES IN THE PACKAGE rather than only in `scripts/`. It began as a script, and a script that
finds this class of defect is a script whose findings arrive after the corpus is finished. The gate
and the audit tool now share one implementation, so what the gate enforces is exactly what the audit
measures -- two copies would drift, and the drift would be invisible in precisely the way this whole
module exists to prevent.

COST, measured: a median of 52 seconds per task, against 5.3 minutes to produce one.
"""
from __future__ import annotations

import io
import json
import os
import re
import tarfile
import time
from typing import Callable

# Directories never worth shipping into the sandbox. `.git` in particular can be larger than
# everything else in the task combined.
SKIP_DIRS = frozenset((".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"))

OPEN_TIMEOUT = 180.0
TRANSFER_TIMEOUT = 600.0
BUILD_TIMEOUT = 1800.0
REPLAY_TIMEOUT = 1200.0

# How many times a build is retried when what failed was the wire.
BUILD_ATTEMPTS = 3

# What a transient build failure looks like. Named rather than "any failure retried", so a Dockerfile
# that is genuinely wrong still fails on the first try and says so.
TRANSPORT_MARKS = (
    "tls: bad record MAC", "TLS handshake timeout", "connection reset by peer",
    "Temporary failure resolving", "Could not connect to", "Connection timed out",
    "i/o timeout", "unexpected EOF", "500 Internal Server Error", "503 Service Unavailable",
    "net/http: TLS handshake", "failed to copy: httpReadSeeker",
)


def tar_bytes(root: str) -> bytes:
    """The task directory as a tar stream, deterministically ordered.

    A tar rather than file-by-file writes, because tar carries the mode bits: `tests/reference/run.sh`
    is executable and a submission whose entry point is not executable does not start.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                full = os.path.join(directory, name)
                archive.add(full, arcname=os.path.relpath(full, root))
    return buffer.getvalue()


def _scale_of(task_dir: str) -> str:
    try:
        with open(os.path.join(task_dir, "task.toml"), encoding="utf-8") as handle:
            for line in handle:
                if line.strip().startswith("scale = "):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _tag_for(name: str) -> str:
    """A docker tag docker will accept.

    Truncating a name can leave a trailing separator and docker rejects the tag outright --
    `invalid tag "frf-replay-matrix-js-sdk-should-use-hydra-for-room-"`. The task was fine; only our
    label for it was not, and it read in the report as a task that would not build.
    """
    cleaned = re.sub(r"[^a-z0-9_.-]", "-", name.lower())[:40].strip("-._")
    return "frf-in-image-%s" % (cleaned or "task")


def _report_in(blob: str) -> dict | None:
    """The graded report inside a blob of output, wherever in it the verifier put it.

    SCANNED AS OBJECTS, NOT AS LINES. The report is pretty-printed, so a line-oriented parse sees
    `{` alone and finds nothing -- which reads as "the verifier said nothing" when it in fact said
    57/57.
    """
    decoder = json.JSONDecoder()
    found = None
    position = 0
    while True:
        start = blob.find("{", position)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(blob, start)
        except ValueError:
            position = start + 1
            continue
        position = end
        if isinstance(value, dict) and "correctness_total" in value:
            found = value


def drive(task_dir: str, *, api_key: str, template: str,
          log: Callable[[str], None] = lambda _m: None) -> dict:
    """Build this task's image, run its verifier in it. -> {ok, passed, total, stage, detail, note}.

    A sandbox that could not be opened comes back as `stage="unavailable"`, not as an exception:
    "we could not ask" is not a verdict about the task, and the caller reads it as inconclusive.
    """
    from e2b import Sandbox

    name = os.path.basename(task_dir.rstrip("/"))
    started = time.monotonic()
    record = {"task": name, "path": task_dir, "ok": False, "stage": "", "detail": "",
              "note": "", "passed": 0, "total": 0, "seconds": 0.0,
              "scale": _scale_of(task_dir)}

    if not os.path.isfile(os.path.join(task_dir, "environment", "Dockerfile")):
        record.update(stage="dockerfile", detail="no environment/Dockerfile to build")
        return record

    sandbox = None
    try:
        try:
            # `int`, because the sandbox lifetime crosses the wire as an int32 and a float is
            # rejected outright: `cannot unmarshal number 3000.0 into ... timeout of type int32`.
            sandbox = Sandbox.create(template=template,
                                     timeout=int(BUILD_TIMEOUT + REPLAY_TIMEOUT),
                                     api_key=api_key, request_timeout=OPEN_TIMEOUT)
        except Exception as why:                           # noqa: BLE001 -- ours, not the task's
            # HANDLED HERE, NOT RAISED UPWARDS. "We could not open a sandbox" is not a verdict about
            # the task, and the pipeline must not have to import this module to learn the difference
            # -- `frf/core` is not allowed to know what an observation looks like, which is also why
            # this file lives under `observe/`.
            record.update(stage="unavailable",
                          detail="could not open a build sandbox: %s" % str(why)[:300])
            return record

        remote = "/tmp/frf-in-image-%d" % (abs(hash(task_dir)) % 10 ** 10)
        sandbox.commands.run("mkdir -p %s" % remote, timeout=30, request_timeout=60)
        sandbox.files.write("%s/task.tar" % remote, tar_bytes(task_dir),
                            request_timeout=TRANSFER_TIMEOUT)
        sandbox.commands.run("tar -xf %s/task.tar -C %s" % (remote, remote),
                             timeout=120, request_timeout=180)
        # THE MOUNT HAS TO BE USABLE BY THE USER THE IMAGE DECLARES. Task images run as `nobody`;
        # this directory is extracted as root, and a submission that cannot write beside its own
        # sources dies at startup. The verifier says so honestly -- "the submission stopped
        # answering" -- and it read as fifty-eight tasks whose expectations did not reproduce. Run
        # as root the first one checked answered 57 of 57.
        #
        # `a+rwX` rather than a chown: which uid the image runs as is the image's business, and this
        # must not need to know it in order to hand over a workspace.
        sandbox.commands.run("chmod -R a+rwX %s" % remote, timeout=120, request_timeout=180)

        tag = _tag_for(name)
        # THE WIRE IS NOT THE MATERIAL, HERE TOO. A build inside DinD reaches the network for a base
        # image, for apt and for npm, and those fail transiently. Counted as failures they say a
        # task is unbuildable when the task is fine.
        record["stage"] = "build"
        built = None
        for attempt in range(BUILD_ATTEMPTS):
            built = sandbox.commands.run(
                "docker build --pull -t %s %s/environment" % (tag, remote),
                timeout=BUILD_TIMEOUT, request_timeout=BUILD_TIMEOUT + 120)
            if built.exit_code == 0:
                break
            output = (built.stdout or "") + (built.stderr or "")
            if attempt < BUILD_ATTEMPTS - 1 and any(m in output for m in TRANSPORT_MARKS):
                log("in-image: build hit the wire, retrying (%d/%d)" % (attempt + 1, BUILD_ATTEMPTS))
                time.sleep(5.0 * (attempt + 1))
                continue
            break
        if built is None or built.exit_code != 0:
            record["detail"] = (((built.stdout or "") + (built.stderr or ""))[-700:]
                                if built is not None else "the build produced no output")
            return record

        # THE TWO SEAMS ARE DRIVEN DIFFERENTLY, and assuming otherwise fails a task that is fine:
        # the repo scale takes `--self-replay` and writes REWARD_PATH, while the call seam takes a
        # workspace pointing at the shipped reference and reports on stdout. Read
        # `observe/call/package.drive` and `observe/checkout_task.drive` together before changing
        # either of these.
        record["stage"] = "replay"
        if record["scale"] == "repo":
            command = ("REWARD_PATH=/tmp/reward.json python3 tests/verify.py "
                       "--task-root tests --workspace environment --self-replay; "
                       "cat /tmp/reward.json 2>/dev/null")
        else:
            command = ("REWARD_PATH=/tmp/reward.json SUBMISSION_ROOT=tests/reference "
                       "python3 tests/verify.py --task-root tests --workspace tests/reference; "
                       "cat /tmp/reward.json 2>/dev/null")
        replay = sandbox.commands.run(
            "docker run --rm -v %s:/task -w /task %s sh -c %s" % (remote, tag, json.dumps(command)),
            timeout=REPLAY_TIMEOUT, request_timeout=REPLAY_TIMEOUT + 120)

        blob = (replay.stdout or "") + "\n" + (replay.stderr or "")
        report = _report_in(blob)
        if report is None:
            record["detail"] = "verifier produced no graded report: " + blob.strip()[-700:]
            return record

        passed = int(report.get("correctness_passed", 0))
        total = int(report.get("correctness_total", 0))
        # THE VERIFIER'S OWN NOTE, CARRIED. Without it every disagreement reads as one sentence for
        # tasks whose causes were not the same thing at all, and the thing that exists to find
        # defects cannot say which defect it found.
        record.update(passed=passed, total=total, note=str(report.get("note", ""))[:400])
        if total <= 0:
            record["detail"] = "the shipped verifier graded nothing inside the delivered image"
        elif passed == total:
            record.update(ok=True, stage="",
                          detail="%d/%d inside the delivered image" % (passed, total))
        else:
            record["detail"] = "%d/%d inside the delivered image%s" % (
                passed, total, (" -- %s" % record["note"]) if record["note"] else "")
        return record
    except Exception as why:                               # noqa: BLE001 -- reported, not raised
        # ONE TASK MUST NOT END THE RUN, and losing this is how a refactor turned a working audit
        # into a crash. The SDK raises `CommandExitException` when a command exits non-zero, so a
        # single task whose `docker build` failed took the whole pool with it and 129 tasks produced
        # no report at all. The same rule the pipeline applies to candidates applies to this.
        record["detail"] = "%s: %s" % (type(why).__name__, " ".join(str(why).split())[-700:])
        return record
    finally:
        record["seconds"] = round(time.monotonic() - started, 1)
        if sandbox is not None:
            try:
                sandbox.kill(request_timeout=30)
            except Exception:                              # noqa: BLE001 -- teardown
                pass


__all__ = ["drive", "tar_bytes", "SKIP_DIRS", "BUILD_ATTEMPTS", "TRANSPORT_MARKS"]
