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

import json
import os
import re
import shlex
import time
import uuid
from typing import Callable

# Directories never worth shipping into the sandbox. `.git` in particular can be larger than
# everything else in the task combined.
SKIP_DIRS = frozenset((".git", ".hg", "__pycache__", ".pytest_cache", ".venv"))

OPEN_TIMEOUT = 180.0
TRANSFER_TIMEOUT = 900.0

# How many times an upload is retried. A repo task's tarball is the largest thing this moves, and a
# single timeout on it silently costs the whole check.
TRANSFER_ATTEMPTS = 3
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
    from ..core.containers import _tar_bytes
    return _tar_bytes(root, exclude=set(SKIP_DIRS), compress=True)


# Lines a build writes when it is explaining itself. A docker build ends with a frame quoting the
# failing RUN and a `note:` telling the reader it is not pip's fault -- true and useless -- while the
# sentence that names the cause is hundreds of lines above it.
_CAUSE_MARKS = ("error:", "ERROR:", "error[", "fatal:", "failed:", "Traceback",
                "not found", "No such file", "Permission denied", "cannot find",
                "requires", "unsupported", "incompatible", "expected")

# Noise that matches the marks above without explaining anything.
_CAUSE_NOISE = ("This error originates from a subprocess", "See above for details",
                "hint: ", "note: This is an issue with the package",
                "See above for output", "A complete log of this run",
                "debconf: ", "requires a controlling tty", "warning: build failed, wait")


def _why_it_failed(output: str, limit: int = 700) -> str:
    """The part of a failed build that says WHY. -> a bounded string.

    THE TAIL IS THE WRONG END and this is the second time that has cost hours. Keeping the last 700
    characters of a docker build keeps the frame that quotes the failing RUN line, which every
    failure has, and drops the message that distinguishes them: five repo tasks all read
    `RUN pip install --no-cache-dir /src: [end of output] ... error: metadata-generation-failed`,
    which names the step and not the cause.
    """
    lines = [" ".join(line.split()) for line in output.splitlines() if line.strip()]
    causes = [line for line in lines
              if any(mark in line for mark in _CAUSE_MARKS)
              and not any(noise in line for noise in _CAUSE_NOISE)]
    picked = causes[:6] if causes else lines[-6:]
    joined = " | ".join(picked)
    return joined[:limit] if len(joined) <= limit else joined[:limit - 3] + "..."



class _Outcome:
    """What a command did, whether the SDK returned it or raised it."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


def _run(sandbox, command: str, *, timeout: float):
    """Run a command and RETURN its outcome, including a failing one.

    The SDK raises `CommandExitException` on a non-zero exit rather than returning it. Written
    against a return value, the build's `exit_code != 0` branch was unreachable: the retry for
    transient network failures never ran, and the message that reached the ledger was the raw
    exception tail rather than the cause this module works to extract. Both looked like they worked.
    """
    try:
        done = sandbox.commands.run(command, timeout=timeout, request_timeout=timeout + 120)
        return _Outcome(int(getattr(done, "exit_code", 0) or 0),
                        getattr(done, "stdout", "") or "", getattr(done, "stderr", "") or "")
    except Exception as why:                               # noqa: BLE001 -- reported as an outcome
        return _Outcome(int(getattr(why, "exit_code", 1) or 1),
                        getattr(why, "stdout", "") or "",
                        (getattr(why, "stderr", "") or "") or str(why))



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


def drive(task_dir: str, *, api_key: str = "", template: str = "", backend=None,
          log: Callable[[str], None] = lambda _m: None, offline: bool = False,
          image_export: Callable | None = None) -> dict:
    """Build this task's image, run its verifier in it. -> {ok, passed, total, stage, detail, note}.

    A sandbox that could not be opened comes back as `stage="unavailable"`, not as an exception:
    "we could not ask" is not a verdict about the task, and the caller reads it as inconclusive.
    """
    name = os.path.basename(task_dir.rstrip("/"))
    if image_export is not None and getattr(backend, 'name', '') != 'remote':
        raise ValueError('image export requires the caller-owned remote backend')
    started = time.monotonic()
    record = {"task": name, "path": task_dir, "ok": False, "stage": "", "detail": "",
              "note": "", "passed": 0, "total": 0, "seconds": 0.0,
              "scale": _scale_of(task_dir)}

    if not os.path.isfile(os.path.join(task_dir, "environment", "Dockerfile")):
        record.update(stage="dockerfile", detail="no environment/Dockerfile to build")
        return record

    # Reference replay can succeed with a mislabeled cross task or overlapping timing inputs.
    # Reject those contradictions before spending a sandbox on a build or exporting an image.
    from pathlib import Path
    import tomllib
    from .artifact_contract import contract_findings
    config_path = Path(task_dir) / 'task.toml'
    try:
        config = tomllib.loads(config_path.read_text()) if config_path.is_file() else {}
        metadata = dict(config.get('metadata', {}), scale=record['scale'])
        contradictions = contract_findings(Path(task_dir), metadata)
    except (OSError, ValueError, TypeError) as error:
        record.update(stage='artifact-contract', detail='invalid task contract: ' + type(error).__name__)
        return record
    if contradictions:
        record.update(stage='artifact-contract', detail=json.dumps(contradictions, sort_keys=True))
        return record

    from ..core import resources
    resources.require_headroom(task_dir)
    sandbox = None
    remote = "/tmp/frf-in-image-" + uuid.uuid4().hex
    tag = _tag_for(name) + "-" + uuid.uuid4().hex[:12]
    container_name = "frf-replay-" + uuid.uuid4().hex

    def run(command, *, timeout):
        if backend is not None:
            done = backend.run(["sh", "-c", command], timeout=timeout)
            return _Outcome(done.exit_code, done.stdout, done.stderr)
        return _run(sandbox, command, timeout=timeout)

    try:
        try:
            # `int`, because the sandbox lifetime crosses the wire as an int32 and a float is
            # rejected outright: `cannot unmarshal number 3000.0 into ... timeout of type int32`.
            if backend is None:
                from e2b import Sandbox
                sandbox = Sandbox.create(template=template,
                                         timeout=int(BUILD_TIMEOUT + REPLAY_TIMEOUT),
                                         api_key=api_key, request_timeout=OPEN_TIMEOUT)
            elif getattr(backend, "name", "") != "remote":
                raise ValueError("delivered-image replay requires a remote backend")
        except Exception as why:                           # noqa: BLE001 -- ours, not the task's
            # HANDLED HERE, NOT RAISED UPWARDS. "We could not open a sandbox" is not a verdict about
            # the task, and the pipeline must not have to import this module to learn the difference
            # -- `frf/core` is not allowed to know what an observation looks like, which is also why
            # this file lives under `observe/`.
            record.update(stage="unavailable",
                          detail="could not open a build sandbox: %s" % str(why)[:300])
            return record

        # THE WIRE IS NOT THE MATERIAL, ON THE WAY IN TOO. A timeout uploading the tarball is not a
        # statement about the task, and swallowing it as INCONCLUSIVE means the task ships with the
        # gate unrun -- which is the same as not having a gate, only quieter.
        if backend is not None:
            backend.push(task_dir, remote, exclude=set(SKIP_DIRS))
        else:
            prepared = run("mkdir -p %s" % remote, timeout=30)
            if prepared.exit_code != 0:
                raise RuntimeError(prepared.output[-700:])
            payload = tar_bytes(task_dir)
            for attempt in range(TRANSFER_ATTEMPTS):
                try:
                    sandbox.files.write("%s/task.tar" % remote, payload,
                                        request_timeout=TRANSFER_TIMEOUT)
                    break
                except Exception:                          # noqa: BLE001 -- bounded retry
                    if attempt == TRANSFER_ATTEMPTS - 1:
                        raise
                    time.sleep(5.0 * (attempt + 1))
            del payload
            unpacked = run("tar -xzf %s/task.tar -C %s" % (remote, remote), timeout=300)
            if unpacked.exit_code != 0:
                raise RuntimeError(unpacked.output[-700:])
        # THE MOUNT HAS TO BE USABLE BY THE USER THE IMAGE DECLARES. Task images run as `nobody`;
        # this directory is extracted as root, and a submission that cannot write beside its own
        # sources dies at startup. The verifier says so honestly -- "the submission stopped
        # answering" -- and it read as fifty-eight tasks whose expectations did not reproduce. Run
        # as root the first one checked answered 57 of 57.
        #
        # `a+rwX` rather than a chown: which uid the image runs as is the image's business, and this
        # must not need to know it in order to hand over a workspace.
        prepared = run("chmod -R a+rwX %s" % remote, timeout=120)
        if prepared.exit_code != 0:
            raise RuntimeError(prepared.output[-700:])

        # THE WIRE IS NOT THE MATERIAL, HERE TOO. A build inside DinD reaches the network for a base
        # image, for apt and for npm, and those fail transiently. Counted as failures they say a
        # task is unbuildable when the task is fine.
        record["stage"] = "build"
        built = None
        for attempt in range(BUILD_ATTEMPTS):
            build_options = '--pull=false --network=none' if offline else '--pull'
            built = run("docker build %s -t %s %s/environment" % (build_options, tag, remote),
                        timeout=BUILD_TIMEOUT)
            if built.exit_code == 0:
                break
            output = built.output
            if attempt < BUILD_ATTEMPTS - 1 and any(m in output for m in TRANSPORT_MARKS):
                log("in-image: build hit the wire, retrying (%d/%d)" % (attempt + 1, BUILD_ATTEMPTS))
                time.sleep(5.0 * (attempt + 1))
                continue
            break
        if built is None or built.exit_code != 0:
            record["detail"] = (_why_it_failed(built.output) if built is not None
                                else "the build produced no output")
            return record

        inspected = run("docker image inspect --format '{{.Id}}' %s" % tag, timeout=30)
        image_id = inspected.stdout.strip()
        if inspected.exit_code != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            record.update(stage="image-identity", detail="could not resolve the built image ID")
            return record
        record["image_id"] = image_id

        from ..core.task_context import workspace_paths
        inventory = workspace_paths(os.path.join(task_dir, 'environment'))
        record['stage'] = 'workspace-inventory'
        check_source = ('import json,os,sys; paths=json.loads(sys.argv[1]); '
                        'missing=[p for p in paths if not os.path.isfile(p) or not os.access(p,os.R_OK)]; '
                        'print(json.dumps({"missing":missing,"uid":os.geteuid()})); '
                        'sys.exit(bool(missing) or os.geteuid()==0)')
        inspected = run('docker run --rm --network=none --entrypoint python3 %s -I -c %s %s' %
                        (image_id, shlex.quote(check_source), shlex.quote(json.dumps(inventory))), timeout=60)
        if inspected.exit_code != 0:
            record['detail'] = 'instruction workspace paths are not available in the image: ' + inspected.output[-1500:]
            return record
        record['workspace_inventory'] = {'checked': len(inventory), 'ok': True}

        verifier_image = image_id
        verifier_tag = tag + '-verifier'
        if os.path.isfile(os.path.join(task_dir, 'tests', 'Dockerfile')):
            record['stage'] = 'verifier-build'
            options = '--pull=false --network=none' if offline else '--pull=false'
            built_verifier = run('docker build %s --build-arg SOLVER_IMAGE=%s -t %s %s/tests' %
                                 (options, tag, verifier_tag, remote), timeout=BUILD_TIMEOUT)
            if built_verifier.exit_code != 0:
                record['detail'] = _why_it_failed(built_verifier.output)
                return record
            inspected_verifier = run("docker image inspect --format '{{.Id}}' %s" % verifier_tag, timeout=30)
            verifier_image = inspected_verifier.stdout.strip()
            if inspected_verifier.exit_code != 0 or not re.fullmatch(r'sha256:[0-9a-f]{64}', verifier_image):
                record['detail'] = 'could not resolve the verifier image ID'
                return record
            record['verifier_image_id'] = verifier_image

        # Process verifiers select the submission through the environment; call verifiers also
        # consume CLI arguments. Preserve the verifier status across reward-file printing.
        record["stage"] = "replay"
        if record["scale"] == "repo":
            command = ("REWARD_PATH=/tmp/reward.json SUBMISSION_ROOT=/task/tests/reference "
                       "python3 tests/verify.py; "
                       "status=$?; cat /tmp/reward.json 2>/dev/null; exit $status")
        else:
            command = ("REWARD_PATH=/tmp/reward.json SUBMISSION_ROOT=tests/reference "
                       "python3 tests/verify.py --task-root tests --workspace tests/reference; "
                       "status=$?; cat /tmp/reward.json 2>/dev/null; exit $status")
        replay = run("docker run --rm --name %s --user 0 --network=none "
                     "-v %s:/task -w /task %s sh -c %s"
                     % (container_name, remote, verifier_image, shlex.quote(command)),
                     timeout=REPLAY_TIMEOUT)

        blob = replay.output
        report = _report_in(blob)
        if report is None:
            record["detail"] = "verifier produced no graded report: " + blob.strip()[-700:]
            return record
        record["report"] = report

        passed = int(report.get("correctness_passed", 0))
        total = int(report.get("correctness_total", 0))
        # THE VERIFIER'S OWN NOTE, CARRIED. Without it every disagreement reads as one sentence for
        # tasks whose causes were not the same thing at all, and the thing that exists to find
        # defects cannot say which defect it found.
        record.update(passed=passed, total=total, note=str(report.get("note", ""))[:400])
        if replay.exit_code != 0:
            record["detail"] = "the verifier exited nonzero: " + record["note"]
        elif total <= 0:
            record["detail"] = "the shipped verifier graded nothing inside the delivered image"
        elif report.get("timing_valid") is not True:
            record["detail"] = "the delivered verifier could not validate timing: " + record["note"]
        elif passed == total:
            record.update(ok=True, stage="",
                          detail="%d/%d inside the delivered image" % (passed, total))
        else:
            record["detail"] = "%d/%d inside the delivered image%s" % (
                passed, total, (" -- %s" % record["note"]) if record["note"] else "")
        if record['ok'] and image_export is not None:
            record['stage'] = 'image-export'
            exported_report = dict(report, verifier_image_id=record['verifier_image_id']) if record.get('verifier_image_id') else report
            record['image_export'] = image_export(image_id, exported_report)
            record['stage'] = ''
        return record
    except Exception as why:                               # noqa: BLE001 -- reported, not raised
        # ONE TASK MUST NOT END THE RUN, and losing this is how a refactor turned a working audit
        # into a crash. The SDK raises `CommandExitException` when a command exits non-zero, so a
        # single task whose `docker build` failed took the whole pool with it and 129 tasks produced
        # no report at all. The same rule the pipeline applies to candidates applies to this.
        record["ok"] = False
        record["detail"] = "%s: %s" % (type(why).__name__, " ".join(str(why).split())[-700:])
        return record
    finally:
        if backend is not None and getattr(backend, "name", "") == "remote":
            failures = []
            commands = ["docker rm -f %s" % container_name]
            if record.get('verifier_image_id'):
                commands.append('docker image rm -f %s-verifier' % tag)
            commands += ["docker image rm -f %s" % tag, "rm -rf %s" % remote]
            for command in commands:
                try:
                    cleaned = run(command, timeout=60)
                    # --rm normally removes the container before this final cleanup.
                    absent = ((command.startswith("docker rm ") and "No such container" in cleaned.output)
                              or (command.startswith("docker image rm ") and "No such image" in cleaned.output))
                    if cleaned.exit_code and not absent:
                        failures.append(cleaned.output[-300:])
                except Exception as why:
                    failures.append(type(why).__name__)
            record["cleanup_ok"] = not failures
            if failures:
                record['cleanup_errors'] = failures
                if record['ok']:
                    record.update(ok=False, stage="cleanup", detail="; ".join(failures))
        record["seconds"] = round(time.monotonic() - started, 1)
        if sandbox is not None:
            try:
                sandbox.kill(request_timeout=30)
                record['cleanup_ok'] = True
            except Exception as why:
                record['cleanup_ok'] = False
                record['cleanup_errors'] = [type(why).__name__]
                if record['ok']:
                    record.update(ok=False, stage='cleanup', detail='could not close replay sandbox')


__all__ = ["drive", "tar_bytes", "SKIP_DIRS", "BUILD_ATTEMPTS", "TRANSPORT_MARKS"]
