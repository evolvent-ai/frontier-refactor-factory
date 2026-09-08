"""The two real sandboxes: a local Docker daemon, and a remote one.

`sandbox.py` says what a sandbox must do and why anything above it must not learn which one it got.
This is the other half -- the two that actually isolate, written against that same small interface.

WHY TWO AND NOT ONE. They fail in different places and neither is a superset. Docker is free, fast
and has whatever the host's kernel has; it is also unavailable inside most containers, which is
where this factory increasingly runs. A remote sandbox works from anywhere and costs money per
minute, and its filesystem starts empty every time. Having both means the pipeline is not hostage to
where it happens to be started from, and `sandbox.find()` can prefer the cheap one honestly.

WHAT IS SHARED, AND WHY IT IS SHARED HERE. Both push a directory in, run commands, and pull a
directory out, and both have to do it over a transport that only carries bytes. So both need the
same tar-stream plumbing, and writing it twice is how the two backends develop different opinions
about symlinks. It lives once, below both.

CREDENTIALS GO IN AS ENVIRONMENT, NEVER AS A PUSHED FILE. A pushed file lands on a disk this process
does not own and can travel home inside a pulled artefact; an environment variable lives as long as
the command does. `credentials.for_sandbox()` is the only source, and neither backend below has any
other way to obtain one.
"""
from __future__ import annotations

import io
import hashlib
import math
from pathlib import Path, PurePosixPath
import gzip
from contextlib import nullcontext
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import re
import uuid

from . import credentials
from . import scratch as _scratch
from .sandbox import Result, SandboxError

# What a sandbox starts from when nothing else is asked for. A slim image with a real package
# manager: the alternative is a minimal image where every task's first act is to discover it has no
# tar, and a build failure that is really a base-image failure is the hardest kind to read.
DEFAULT_IMAGE = "python:3.11-slim-bookworm"

# Paths never copied into or out of a sandbox. Version-control metadata and build caches are large,
# and worse, they carry absolute paths from the machine that made them -- which is exactly the kind
# of host detail that must not end up frozen into an expectation.
EXCLUDED = {".git", ".hg", "__pycache__", ".pytest_cache", ".venv", "node_modules", "target"}

# HOW LONG TO WAIT ON THE WIRE, which is a DIFFERENT question from how long to let a command run.
# The E2B SDK takes both and they are easy to conflate: `timeout` bounds the process inside the
# sandbox, `request_timeout` bounds the HTTP call that carries it. Passing only the first leaves the
# second at None, which means wait forever.
#
# THAT COST A BATCH. A kernel/java run sat for 28 minutes with its main thread in futex_wait and an
# ESTAB socket to the API, having widened its candidates and never reached a build. Worse, it made the
# retry logic below unreachable for the failure it was written for: a call that never returns never
# raises, so `re.search("timed out", ...)` never sees anything to match.
#
# OPEN_TIMEOUT covers creating and connecting a sandbox -- an operation with a bounded amount of work
# to do, however busy the service is.
OPEN_TIMEOUT = 120

# TRANSFER_TIMEOUT covers moving a file, which is a tar of a whole checkout in the worst case.
TRANSFER_TIMEOUT = 300

# And for running a command the transport limit has to EXCEED the command's own, or a legitimate long
# build would be cut off mid-flight by the wire that was carrying it -- reported as a transport fault
# for what is really a slow compile. This adds headroom rather than replacing the caller's figure.
TRANSPORT_HEADROOM = 120

# Killing a sandbox is bounded SHORT, because failing it costs nothing: an unkilled sandbox expires on
# its own, which is what the handler in `close` already relies on. Waiting is the expensive outcome
# here, not giving up -- a batch whose work was finished sat in teardown for eleven minutes.
TEARDOWN_TIMEOUT = 30

# How many times opening a sandbox is retried before the candidate is given up on.
# See the loop in `Remote.__init__` for why three was too few.
OPEN_ATTEMPTS = 6

# HOW LONG A SANDBOX STAYS ALIVE, and it has to exceed the longest stage that runs inside one.
#
# It did not. This defaulted to 3600 -- exactly `FRF_FREEZE_MAX_SECONDS` -- so a freeze allowed to
# take its full hour was racing the very sandbox holding it, and the sandbox always won: the freeze
# still had work to do when its container expired underneath it. The package scale is where that
# bites, because compiling the subject in the sandbox (which this scale only started doing at all
# recently) put real minutes into every one of the five runs.
#
# WHAT IT LOOKED LIKE, and why the equality was easy to miss: `The sandbox was not found: This error
# is likely due to sandbox timeout` after 3616 seconds. Not a crash, not the material -- a deadline
# that was set equal to the work instead of around it. The candidate is charged to FACTORY, which is
# right, but the fix is ordering rather than attribution.
#
# The headroom is the same idea as TRANSPORT_HEADROOM: an inner bound must be strictly inside the
# outer one, or the outer one is what the caller actually gets.
# WHAT THE API WILL ACTUALLY ACCEPT. E2B answers `400: Timeout cannot be greater than 1 hours`, so
# a longer lifetime is not a longer sandbox -- it is no sandbox. The default was 5400 and every
# creation that used it was refused: twelve in one repo batch, none of them reaching the ledger,
# because a sandbox that never opened is not a candidate that failed.
#
# Clamped rather than merely defaulted, so an environment that asks for more gets the most it can
# have instead of nothing at all.
SANDBOX_LIFETIME_CEILING = 3600.0
SANDBOX_LIFETIME = min(float(os.environ.get("FRF_SANDBOX_LIFETIME", "3540")),
                       SANDBOX_LIFETIME_CEILING)


class _CommandTimedOut(Exception):
    """The command outlived the deadline this process set for it.

    Shaped like the SDK's own timeout so the retry below reads it the same way: no `exit_code`, and
    a message the transport-timeout pattern matches.
    """


def _wait_bounded(handle, seconds: float):
    """Wait for a backgrounded command, against a deadline WE hold. -> its `CommandResult`.

    THE RESULT, NOT THE HANDLE. `CommandHandle` carries no `stdout`, `stderr` or `exit_code` --
    those live on the `CommandResult` that `wait()` returns, which is also what the foreground
    `run()` hands back. Returning the handle would leave every caller reading "" from `getattr`
    and seeing a silent success for commands that printed, failed, or both.

    WHY NOT `handle.wait()` ON ITS OWN. It takes no timeout -- only callbacks -- and iterates the
    event stream until the far side ends it. The sandbox's own `timeout` normally does end it, by
    killing the command so the stream closes, and that is the first line of defence. This is the
    second: if the stream stalls without the command dying -- a dropped connection that never
    resets, a sandbox that expired underneath us -- nothing in the SDK ever returns, and a batch
    stops with no output, no error, and nothing naming the candidate that did it.

    That failure has a history here. `TRANSPORT_HEADROOM` exists because a call that never returns
    also never raises, so the retry written for it was unreachable; this is the same lesson applied
    one layer further in.

    The wait runs on a worker thread because the SDK's iteration is synchronous and cannot be
    interrupted. On expiry the command is killed so the sandbox does not keep running work whose
    result nobody will read; the thread is left to die with the stream it is blocked on.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frf-e2b-wait")
    try:
        future = pool.submit(handle.wait)
        try:
            # A non-zero exit raises CommandExitException inside the worker, and `result()`
            # re-raises it here -- so the caller's existing translation of that exception into a
            # Result keeps working exactly as it did on the foreground path.
            return future.result(timeout=max(1.0, seconds))
        except _FuturesTimeout:
            try:
                handle.kill()
            except Exception:                                 # noqa: BLE001 -- best effort
                pass
            raise _CommandTimedOut(
                "the sandbox command exceeded its %.0fs request timeout and was killed" % seconds)
    finally:
        # NOT `shutdown(wait=True)`: the worker may still be blocked in the stream, and waiting for
        # it here would reintroduce exactly the unbounded wait this function exists to remove.
        pool.shutdown(wait=False)


def _archive_size(local_dir: str, excluded: set) -> int:
    estimate = 0
    for root, dirs, files in os.walk(local_dir):
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in dirs + files:
            if name not in excluded:
                path = os.path.join(root, name)
                estimate += 2048
                if not os.path.islink(path) and os.path.isfile(path):
                    estimate += os.path.getsize(path)
    return estimate


def _write_tar(local_dir: str, buffer, excluded: set, *, compress: bool) -> None:
    stream = gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0, compresslevel=1) if compress else nullcontext(buffer)
    with stream as output:
        with tarfile.open(fileobj=output, mode="w|") as archive:
            for root, dirs, files in os.walk(local_dir):
                dirs[:] = sorted(d for d in dirs if d not in excluded)
                for name in sorted(dirs + files):
                    if name in excluded:
                        continue
                    full = os.path.join(root, name)
                    info = archive.gettarinfo(full, arcname=os.path.relpath(full, local_dir))
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    if info.isfile():
                        with open(full, "rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


def _tar_bytes(local_dir: str, exclude: set | None = None, *, compress: bool = False) -> bytes:
    """Deterministic in-memory archive for local callers with explicit memory admission."""
    excluded = EXCLUDED if exclude is None else exclude
    from . import resources
    resources.require_headroom(local_dir, transfer_bytes=_archive_size(local_dir, excluded))
    buffer = io.BytesIO()
    _write_tar(local_dir, buffer, excluded, compress=compress)
    return buffer.getvalue()


def _untar_bytes(blob: bytes, local_dir: str) -> None:
    return _untar_stream(io.BytesIO(blob), local_dir)


def _untar_stream(stream, local_dir: str) -> None:
    """Unpack a tar stream, refusing anything that would write outside the destination.

    The check is not paranoia about a hostile registry: the streams here come back from a sandbox
    that just ran code we did not write, and a member named `../../etc/something` is exactly what a
    submission trying to escape the measurement would produce.

    CHECKING EACH MEMBER'S OWN PATH IS NOT ENOUGH, and the first version of this did only that. A
    tar can carry a SYMLINK whose own name is innocent -- `escape` -> `/tmp/victim` -- followed by a
    perfectly ordinary `escape/note.txt`, and the second member is then written through the first,
    outside the destination, with every path check satisfied. Links are therefore dropped outright:
    nothing this factory pulls back from a sandbox needs one, and a link is the only member whose
    meaning depends on what was extracted before it.
    """
    os.makedirs(local_dir, exist_ok=True)
    destination = os.path.abspath(local_dir)
    with tarfile.open(fileobj=stream, mode="r:*") as archive:
        safe = []
        # The names that survived the filter, so a hard link can be checked against what will really
        # be written rather than against what the archive merely lists. `tar` emits the first copy of
        # a file as a regular member and later ones as links to it, so a kept target always precedes
        # its links here.
        kept = set()
        for member in archive.getmembers():
            # SYMLINKS STAY DROPPED, HARD LINKS DO NOT, and collapsing the two lost real programs.
            # The reasoning above is about symlinks: their target is a PATH resolved at write time,
            # so `escape -> /tmp/victim` followed by `escape/note.txt` writes outside the
            # destination. A hard link cannot do that -- it names another MEMBER OF THIS ARCHIVE,
            # which is validated by the same loop before anything is written.
            #
            # WHAT DROPPING THEM COST. `cargo build --release` hard-links `target/release/<name>`
            # from the artefact it built in `target/release/deps/`, so the linked binary travels as
            # a hard-link member -- and was silently discarded on the way out of the sandbox. The
            # delivered task then contained `tr-lang.d`, `libtr_lang.d`, `build/`, `examples/` and
            # NO `tr-lang`: every sibling of the program except the program. Its `run.sh` pointed at
            # a path that did not exist, so the shipped verifier saw empty stdout and a non-zero
            # exit on all 57 scenarios, and the task was refused for an image that could not run
            # its own reference. Measured on one finished batch: 22 tasks whose frozen corpora were
            # all form-OK, every one blocked here.
            if member.issym():
                continue
            target = os.path.abspath(os.path.join(destination, member.name))
            if not (target == destination or target.startswith(destination + os.sep)):
                continue
            if member.islnk():
                # The link's own name is inside the destination; its TARGET must be too, and must be
                # a member we are actually keeping. A hard link to a member that was itself rejected
                # has nothing to point at, and `tarfile` raises `LinkFallbackError` for the whole
                # extraction rather than skipping it -- so one dropped symlink with an alias beside it
                # would lose the entire pull. Checked against `kept` rather than the archive listing
                # because being present is not the same as surviving this filter.
                linked = os.path.abspath(os.path.join(destination, member.linkname))
                if not linked.startswith(destination + os.sep) or member.linkname not in kept:
                    continue
            kept.add(member.name)
            safe.append(member)
        # `filter="data"` is the interpreter's own version of this reasoning and is used where it
        # exists; the explicit checks above stay because they are what runs on 3.10 and 3.11, which
        # this package supports.
        try:
            archive.extractall(destination, members=safe, filter="data")
        except TypeError:                                  # pragma: no cover -- Python < 3.11.4
            archive.extractall(destination, members=safe)


class Docker:
    """A container on a local Docker daemon.

    Kept alive across the whole stage rather than started per command, for the same reason the call
    seam keeps one subject alive across a corpus: a build followed by five freezes is dozens of
    commands, and paying container startup for each would dominate what is being measured.

    Driven through the `docker` CLI rather than the HTTP API on purpose. The CLI is what is present
    wherever a daemon is, it needs no client library -- this package has no runtime dependencies --
    and its failures are the ones an operator can reproduce by hand from the log.
    """

    name = "docker"

    def __init__(self, image: str = DEFAULT_IMAGE, *, workdir: str = "/work",
                 network: str = "none", memory: str = "4g", cpus: str = "2") -> None:
        self.image = image
        self.workdir = workdir
        # OFFLINE BY DEFAULT. A subject that can reach the network can download the answer, and a
        # measurement taken while a package index is being consulted is a measurement of the
        # network. Callers that genuinely need to fetch something -- a build stage -- ask for it.
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self._id = ""

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "Docker":
        if self._id:
            return self
        name = "frf-%s" % uuid.uuid4().hex[:12]
        argv = ["docker", "run", "--detach", "--name", name,
                "--network", self.network, "--memory", self.memory, "--cpus", self.cpus,
                # A submission that forks without bound would otherwise take the host down with it,
                # and the failure would look like the factory being flaky.
                "--pids-limit", "512",
                "--workdir", self.workdir, self.image,
                "sleep", "infinity"]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            raise SandboxError("could not start a container from %r: %s"
                               % (self.image, done.stderr.strip()[-500:]))
        self._id = done.stdout.strip()
        self.run(["mkdir", "-p", self.workdir], timeout=60)
        return self

    def close(self) -> None:
        if not self._id:
            return
        subprocess.run(["docker", "rm", "--force", self._id],
                       capture_output=True, text=True, timeout=120)
        self._id = ""

    def __enter__(self) -> "Docker":
        return self.start()

    def __exit__(self, *_) -> None:
        self.close()

    # ---------------------------------------------------------------- the interface
    def push(self, local_dir: str, remote_dir: str, *, exclude: set | None = None) -> None:
        self.start()
        self.run(["mkdir", "-p", remote_dir], timeout=60)
        done = subprocess.run(["docker", "cp", "-", "%s:%s" % (self._id, remote_dir)],
                              input=_tar_bytes(local_dir, exclude=exclude),
                              capture_output=True, timeout=900)
        if done.returncode != 0:
            raise SandboxError("could not copy into the container: %s"
                               % done.stderr.decode("utf-8", "replace")[-500:])

    def run(self, argv: list[str], *, workdir: str | None = None,
            env: dict | None = None, timeout: float = 3600.0) -> Result:
        self.start()
        command = ["docker", "exec", "--workdir", workdir or self.workdir]
        for key, value in (env or {}).items():
            # As arguments to exec, so they live exactly as long as this command does. Written to a
            # file inside the container they would outlive it and could be pulled back out.
            command += ["--env", "%s=%s" % (key, value)]
        command += [self._id] + list(argv)

        started = time.perf_counter()
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return Result(-1, _text(exc.stdout), _text(exc.stderr) + "\n[timed out]",
                          time.perf_counter() - started)
        return Result(done.returncode, done.stdout, done.stderr, time.perf_counter() - started)

    def pull(self, remote_dir: str, local_dir: str) -> None:
        self.start()
        done = subprocess.run(["docker", "cp", "%s:%s/." % (self._id, remote_dir), "-"],
                              capture_output=True, timeout=900)
        if done.returncode != 0:
            raise SandboxError("could not copy out of the container: %s"
                               % done.stderr.decode("utf-8", "replace")[-500:])
        _untar_bytes(done.stdout, local_dir)


class Remote:
    """A sandbox somewhere else, reached through the `e2b` SDK.

    The import is deferred to construction rather than to module import, so that a machine without
    the extra installed can still read `sandbox.available()` and be told what it is missing -- the
    diagnostic must not be the thing that fails.

    A remote sandbox has a lifetime measured in tens of minutes and then evaporates with everything
    on it. That is a property to design around rather than to fight: anything worth keeping is
    pulled when it is produced, not at the end of a batch, because at the end of a batch it may no
    longer exist.
    """

    name = "remote"

    def runtime(self, recipe):
        """Reuse a source-runtime container; lifecycle belongs to this E2B sandbox."""
        if not hasattr(self, '_source_runtimes'):
            self._source_runtimes = {}
        if recipe not in self._source_runtimes:
            self._source_runtimes[recipe] = RemoteImage(self, recipe)
        return self._source_runtimes[recipe]

    def __init__(self, template: str = "", *, timeout: float = SANDBOX_LIFETIME) -> None:
        try:
            from e2b import Sandbox                                        # noqa: PLC0415
        except ImportError as exc:
            raise SandboxError(
                "the remote sandbox needs the e2b package: pip install 'frontier-refactor-factory"
                "[sandbox]'. Refusing to fall back to this host: an expectation frozen here would "
                "describe this machine.") from exc

        key = credentials.get("E2B_API_KEY")
        if not key:
            raise SandboxError("E2B_API_KEY is not set, so no remote sandbox can be opened.")
        # `Sandbox.create`, not `Sandbox(...)`. In the 2.x SDK the constructor takes an internal
        # options object and calling it as though it took keywords fails at the first use -- which
        # is a poor place to find out, because the first use is the most expensive stage.
        # The DinD template setup script and the validation tooling use this name. Keep the
        # older generic spelling as a compatibility fallback for manually configured deployments.
        self._template = (template or credentials.get("E2B_DIND_TEMPLATE")
                          or credentials.get("E2B_TEMPLATE") or "")
        # RETRIED FOR AS LONG AS THE OUTAGE PLAUSIBLY LASTS. Three attempts at one and two seconds
        # tolerates three seconds of trouble, and the failures this guards against are not that
        # short: a live batch died on `dns error: request timed out` reaching api.e2b.app, which is
        # a resolver blip measured in tens of seconds. Six attempts backing off to thirty seconds
        # covers about a minute and a half, and costs nothing when the first attempt succeeds.
        for attempt in range(OPEN_ATTEMPTS):
            try:
                if self._template:
                    self._sandbox = Sandbox.create(template=self._template, timeout=int(timeout),
                                                   api_key=key, request_timeout=OPEN_TIMEOUT)
                else:
                    self._sandbox = Sandbox.create(timeout=int(timeout), api_key=key,
                                                   request_timeout=OPEN_TIMEOUT)
                break
            except Exception as exc:                          # noqa: BLE001 -- SDK transport/errors
                message = str(exc)
                transient = bool(re.search(r"dns|connect|connection|temporar|timed out|timeout|"
                                           r"5\d\d|no connections", message, re.I))
                if not transient or attempt == OPEN_ATTEMPTS - 1:
                    raise SandboxError(
                        "could not open a remote sandbox%s: %s"
                        % (" from template %r" % self._template if self._template else "", message)) from exc
                time.sleep(min(2 ** attempt, 30))

    def close(self) -> None:
        for runtime in getattr(self, '_source_runtimes', {}).values():
            try:
                runtime.close()
            except Exception:
                pass
        self._source_runtimes = {}
        try:
            # BOUNDED LIKE EVERY OTHER REMOTE CALL, and this one needed saying twice: the `except`
            # below looks like it covers anything teardown can do wrong, and it does not cover the
            # thing that actually happens. An unbounded call does not raise, it WAITS -- so a batch
            # that had finished its work sat in cleanup with a live socket to the API and no output,
            # and the handler underneath never ran. TEARDOWN_TIMEOUT is short on purpose, because
            # failing it is harmless: an unkilled sandbox expires on its own.
            self._sandbox.kill(request_timeout=TEARDOWN_TIMEOUT)
        except Exception:                                     # noqa: BLE001 -- teardown, not a run
            # A sandbox that cannot be killed will expire on its own. Raising here would turn a
            # successful build into a failure during cleanup, which is the worst kind of false
            # negative: the work was done and the report says it was not.
            pass

    def __enter__(self) -> "Remote":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def push(self, local_dir: str, remote_dir: str, *, exclude: set | None = None) -> None:
        """Spool a compressed archive to disk and send bounded, deadline-limited chunks."""
        from . import resources
        excluded = EXCLUDED if exclude is None else exclude
        staged = "/tmp/frf-push-%s.tar.gz" % uuid.uuid4().hex
        parts = []
        with resources.transfer_slot(local_dir, _scratch.base()):
            estimate = _archive_size(local_dir, excluded)
            resources.require_headroom(local_dir, _scratch.base(), disk_bytes=estimate + estimate // 100)
            try:
                with tempfile.TemporaryFile(dir=_scratch.base()) as archive:
                    _write_tar(local_dir, archive, excluded, compress=True)
                    archive.seek(0)
                    while True:
                        chunk = archive.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        resources.require_headroom(local_dir, _scratch.base(), transfer_bytes=len(chunk))
                        part = staged + '.%06d' % len(parts)
                        parts.append(part)
                        for attempt in range(3):
                            try:
                                self._sandbox.files.write(part, chunk, request_timeout=TRANSFER_TIMEOUT)
                                break
                            except Exception as error:
                                transient = bool(re.search(
                                    r"timed? ?out|timeout|request.+error|no connections",
                                    str(error), re.I))
                                if transient and attempt < 2:
                                    time.sleep(1.5 ** attempt)
                                    continue
                                raise SandboxError('remote archive upload failed: %s' %
                                                   type(error).__name__) from error
                    del chunk
                command = ("cat -- %s > %s && mkdir -p %s && tar -xf %s -C %s" %
                           (" ".join(_quote(part) for part in parts), _quote(staged),
                            _quote(remote_dir), _quote(staged), _quote(remote_dir)))
                done = self.run(['sh', '-c', command], timeout=900)
                if not done.ok:
                    raise SandboxError('could not unpack into the sandbox: %s' % done.tail())
            finally:
                self.run(['rm', '-f', '--', staged] + parts, timeout=60)

    def run(self, argv: list[str], *, workdir: str | None = None,
            env: dict | None = None, timeout: float = 3600.0) -> Result:
        """One command. -> its exit code and streams, exactly as every other backend reports them.

        A NON-ZERO EXIT IS NOT AN ERROR HERE, and this SDK disagrees: it raises
        `CommandExitException` for any command that exits non-zero, so the ordinary case of "the
        build failed, read the message" arrives as an exception. Letting that propagate would make
        `Result.ok` unreachable on this backend and every caller need a second code path for the
        remote case. Verified live: `commands.run("false")` raises rather than returning 1.

        The exception carries the exit code and both streams, so the honest translation is back into
        a Result. A failure with no exit code to report is a transport problem and keeps 1.
        """
        started = time.perf_counter()
        command = " ".join(_quote(part) for part in argv)
        for attempt in range(2):
            try:
                handle = self._sandbox.commands.run(
                    command, cwd=workdir or "/home/user", envs=dict(env or {}),
                    timeout=int(timeout),
                    # Headroom over the command's own limit, so a slow build is cut off by the
                    # limit it was given and not by the wire carrying it. See TRANSPORT_HEADROOM.
                    request_timeout=int(timeout) + TRANSPORT_HEADROOM,
                    # START IT, DO NOT WAIT FOR IT HERE. Waiting is done below against a deadline
                    # this process owns -- see `_wait_bounded` for why the SDK's own wait is not
                    # enough on its own.
                    background=True)
                # Rebound to the RESULT -- the lines below read exit_code/stdout/stderr off it,
                # and a CommandHandle carries none of the three.
                handle = _wait_bounded(handle, int(timeout) + TRANSPORT_HEADROOM)
                break
            except Exception as exc:                          # noqa: BLE001 -- the SDK's own errors
                code = getattr(exc, "exit_code", None)
                message = str(exc)
                transport_timeout = code is None and bool(re.search(r"request.?timeout|timed out",
                                                                      message, re.I))
                if transport_timeout and attempt == 0:
                    time.sleep(1.0)
                    continue
                return Result(1 if code is None else int(code),
                              _text(getattr(exc, "stdout", "")),
                              _text(getattr(exc, "stderr", "")) or message[-2000:],
                              time.perf_counter() - started)
        return Result(getattr(handle, "exit_code", 0), _text(getattr(handle, "stdout", "")),
                      _text(getattr(handle, "stderr", "")), time.perf_counter() - started)

    def pull(self, remote_dir: str, local_dir: str) -> None:
        """Download a compressed archive as a bounded stream, without buffering the tree."""
        from . import resources
        staged = "/tmp/frf-pull-%s.tar.gz" % uuid.uuid4().hex
        with resources.transfer_slot(local_dir, _scratch.base()):
            try:
                done = self.run(['tar', '-czf', staged, '-C', remote_dir, '.'], timeout=900)
                if not done.ok:
                    raise SandboxError('could not pack the sandbox directory: %s' % done.tail())
                size = self.run(['stat', '-c', '%s', staged], timeout=30)
                expanded = self.run(['du', '-sb', '--', remote_dir], timeout=60)
                if not size.ok or not expanded.ok:
                    raise SandboxError('could not measure remote archive size')
                compressed_bytes = int(size.stdout.strip())
                expanded_bytes = int(expanded.stdout.split()[0])
                resources.require_headroom(local_dir, _scratch.base(),
                                           disk_bytes=compressed_bytes + expanded_bytes)
                with tempfile.TemporaryFile(dir=_scratch.base()) as archive:
                    for attempt in range(3):
                        archive.seek(0)
                        archive.truncate()
                        received = 0
                        checked_at = 0
                        try:
                            with self._sandbox.files.read(staged, format='stream',
                                                         request_timeout=TRANSFER_TIMEOUT) as stream:
                                for chunk in stream:
                                    received += len(chunk)
                                    if received > compressed_bytes:
                                        raise SandboxError('remote archive grew during download')
                                    archive.write(chunk)
                                    if received - checked_at >= 16 * 1024 * 1024:
                                        resources.require_headroom(local_dir, _scratch.base(),
                                                                   disk_bytes=expanded_bytes)
                                        checked_at = received
                            if received != compressed_bytes:
                                raise SandboxError('incomplete remote archive download')
                            break
                        except Exception as error:
                            transient = bool(re.search(
                                r"timed? ?out|timeout|request.+error|no connections",
                                str(error), re.I))
                            if transient and attempt < 2:
                                time.sleep(1.5 ** attempt)
                                continue
                            raise
                    archive.seek(0)
                    _untar_stream(archive, local_dir)
            finally:
                self.run(['rm', '-f', '--', staged], timeout=60)


def _quote(part: str) -> str:
    """Shell-quote one argument.

    The remote SDK takes a command line rather than an argv, so the argv this interface promises has
    to be rendered back into one. Doing it by joining on spaces would break the first path with a
    space in it and, worse, would let a filename decide where an argument ends.
    """
    import shlex
    return shlex.quote(part)


def _text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def docker_available() -> bool:
    """Whether a daemon is actually reachable, rather than whether the CLI is installed.

    The distinction is the whole point: `docker` on PATH inside a container without a socket is the
    commonest false positive there is, and it turns "no sandbox" into a confusing failure at the
    most expensive stage instead of a clear one before any work starts.
    """
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def scratch() -> str:
    """A temporary directory for staging pushes and pulls."""
    return _scratch.mkdtemp(prefix="frf-stage-")


class RemoteImage:
    name = 'remote'

    def __init__(self, backend, recipe):
        if backend.name != 'remote':
            raise ValueError('source-runtime images require E2B')
        self.control_backend = backend
        self.recipe = recipe
        self.container = 'frf-source-' + uuid.uuid4().hex
        self.started = False
        self.evidence = {'recipe_sha256': hashlib.sha256(recipe.encode()).hexdigest()}

    def _checked(self, argv, timeout=60):
        result = self.control_backend.run(argv, timeout=timeout)
        if not result.ok:
            raise SandboxError('source-runtime operation failed: ' + result.tail(1200))
        return result

    def _ensure(self):
        if self.started:
            return
        remote = '/tmp/' + self.container + '-build'
        try:
            with _scratch.temporary_directory() as local:
                Path(local, 'Dockerfile').write_text(self.recipe)
                self.control_backend.push(local, remote)
            self._checked(['docker', 'build', '--pull', '-t', self.container, remote], timeout=900)
            identity = self._checked(['docker', 'image', 'inspect', '--format', '{{.Id}}',
                                      self.container]).stdout.strip()
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', identity):
                raise SandboxError('source-runtime image identity missing')
            self._checked(['docker', 'run', '-d', '--name', self.container, '--user', '0',
                           '--cpus', '4', '--memory', '4g', '--pids-limit', '512',
                           '--entrypoint', 'sleep', identity, 'infinity'])
            # Validate the toolchain inside the actual long-lived container. A successful Docker
            # build alone is insufficient: a malformed digest or overridden image entrypoint can
            # leave a container that starts but cannot execute the declared compiler.
            probe = self._checked(['docker', 'exec', '--user', '0', self.container,
                                   'sh', '-c', 'command -v python3 || command -v python || true; '
                                   'command -v go || true; command -v rustc || true; '
                                   'command -v cargo || true; command -v gcc || true'], timeout=30)
            available = {Path(value).name for value in probe.stdout.split()}
            languages = set(re.findall(r'\b(python3?|go|rustc|cargo|gcc)\b', self.recipe))
            requirements = ({'python3'} if 'python:' in self.recipe else set())
            if 'golang:' in self.recipe:
                requirements.add('go')
            if 'rust:' in self.recipe:
                requirements.update(('rustc', 'cargo'))
            if 'debian:' in self.recipe and 'gcc' in self.recipe:
                requirements.add('gcc')
            missing = sorted(requirements - available)
            if missing:
                raise SandboxError('source-runtime toolchain missing: ' + ', '.join(missing) +
                                   '; available=' + repr(sorted(available)) +
                                   '; probe=' + repr(probe.stdout[-500:]))
            self.evidence['image_id'] = identity
            self.started = True
        except BaseException:
            self.close()
            raise
        finally:
            self.control_backend.run(['rm', '-rf', '--', remote], timeout=30)

    @staticmethod
    def _path(path):
        value = PurePosixPath(path)
        if not value.is_absolute() or '..' in value.parts or str(value) == '/':
            raise ValueError('source-runtime transfer requires an absolute non-root path')
        return str(value)

    def push(self, local_dir, remote_dir, *, exclude=None):
        destination = self._path(remote_dir)
        self._ensure()
        staged = '/tmp/frf-runtime-transfer-' + uuid.uuid4().hex
        try:
            self.control_backend.push(local_dir, staged, exclude=exclude)
            self._checked(['docker', 'exec', self.container, 'mkdir', '-p', destination])
            self._checked(['docker', 'cp', staged + '/.', self.container + ':' + destination], timeout=300)
        finally:
            self.control_backend.run(['rm', '-rf', '--', staged], timeout=30)

    def pull(self, remote_dir, local_dir):
        source = self._path(remote_dir)
        self._ensure()
        staged = '/tmp/frf-runtime-transfer-' + uuid.uuid4().hex
        try:
            self._checked(['mkdir', '-p', staged])
            self._checked(['docker', 'cp', self.container + ':' + source + '/.', staged], timeout=300)
            self.control_backend.pull(staged, local_dir)
        finally:
            self.control_backend.run(['rm', '-rf', '--', staged], timeout=30)

    def run(self, argv, *, workdir=None, env=None, timeout=3600):
        self._ensure()
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('source-runtime command requires a positive timeout')
        # Source builds must see the declared toolchain regardless of the base image's default
        # runtime user or PATH. The task itself is later replayed under its declared user; this
        # container is only the controlled reference-build environment.
        command = ['docker', 'exec', '--user', '0', '--workdir', workdir or '/app']
        command += ['--env', 'PATH=/usr/local/go/bin:/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin']
        command += ['--env', 'GOROOT=/usr/local/go', '--env', 'GOPATH=/go']
        for key, value in (env or {}).items():
            command += ['--env', str(key) + '=' + str(value)]
        # Resolve toolchain entrypoints explicitly. `timeout` uses execvp inside the image, and
        # some official images expose /usr/local/go/bin through ENV only in their default shell;
        # the non-interactive timeout child does not inherit that shell lookup reliably.
        if argv and argv[0] == 'go':
            argv = ['/usr/local/go/bin/go', *argv[1:]]
        elif argv and argv[0] in ('rustc', 'cargo'):
            argv = ['/usr/local/cargo/bin/' + argv[0], *argv[1:]]
        # An E2B command deadline alone kills docker exec's client, not its container process.
        command += [self.container, 'timeout', '--kill-after=5s', str(timeout) + 's', *argv]
        return self.control_backend.run(command, timeout=timeout + 10)

    def close(self):
        self.control_backend.run(['docker', 'rm', '-f', self.container], timeout=30)
        self.control_backend.run(['docker', 'image', 'rm', self.container], timeout=30)
        self.started = False
