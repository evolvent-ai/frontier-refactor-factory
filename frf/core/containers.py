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
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

from . import credentials
from .sandbox import Result, SandboxError

# What a sandbox starts from when nothing else is asked for. A slim image with a real package
# manager: the alternative is a minimal image where every task's first act is to discover it has no
# tar, and a build failure that is really a base-image failure is the hardest kind to read.
DEFAULT_IMAGE = "python:3.11-slim-bookworm"

# Paths never copied into or out of a sandbox. Version-control metadata and build caches are large,
# and worse, they carry absolute paths from the machine that made them -- which is exactly the kind
# of host detail that must not end up frozen into an expectation.
EXCLUDED = {".git", ".hg", "__pycache__", ".pytest_cache", ".venv", "node_modules", "target"}


def _tar_bytes(local_dir: str) -> bytes:
    """A directory as a tar stream, with the host left out of it.

    Names are stored relative to the directory root so that unpacking cannot depend on where the
    directory happened to live, and mtimes are flattened so that pushing the same tree twice
    produces the same bytes -- which is what makes a cached layer or a diff meaningful.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for root, dirs, files in os.walk(local_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDED]
            for name in files:
                if name in EXCLUDED:
                    continue
                full = os.path.join(root, name)
                info = archive.gettarinfo(full, arcname=os.path.relpath(full, local_dir))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(full, "rb") as handle:
                    archive.addfile(info, handle)
    return buffer.getvalue()


def _untar_bytes(blob: bytes, local_dir: str) -> None:
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
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
        safe = []
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                continue
            target = os.path.abspath(os.path.join(destination, member.name))
            if target == destination or target.startswith(destination + os.sep):
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
    def push(self, local_dir: str, remote_dir: str) -> None:
        self.start()
        self.run(["mkdir", "-p", remote_dir], timeout=60)
        done = subprocess.run(["docker", "cp", "-", "%s:%s" % (self._id, remote_dir)],
                              input=_tar_bytes(local_dir), capture_output=True, timeout=900)
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

    def __init__(self, template: str = "", *, timeout: float = 3600.0) -> None:
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
        try:
            if self._template:
                self._sandbox = Sandbox.create(template=self._template, timeout=int(timeout),
                                               api_key=key)
            else:
                self._sandbox = Sandbox.create(timeout=int(timeout), api_key=key)
        except Exception as exc:                              # noqa: BLE001 -- the SDK's own errors
            raise SandboxError(
                "could not open a remote sandbox%s: %s"
                % (" from template %r" % self._template if self._template else "", exc)) from exc

    def close(self) -> None:
        try:
            self._sandbox.kill()
        except Exception:                                     # noqa: BLE001 -- teardown, not a run
            # A sandbox that cannot be killed will expire on its own. Raising here would turn a
            # successful build into a failure during cleanup, which is the worst kind of false
            # negative: the work was done and the report says it was not.
            pass

    def __enter__(self) -> "Remote":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def push(self, local_dir: str, remote_dir: str) -> None:
        """Upload as one tar and unpack there.

        File by file would be one network round trip per file, and a repository is thousands. The
        remote end is asked to unpack with its own tar rather than by the SDK, because the SDK's
        write API takes bytes and has no opinion about archives.
        """
        blob = _tar_bytes(local_dir)
        staged = "/tmp/frf-push-%s.tar" % uuid.uuid4().hex[:8]
        self._sandbox.files.write(staged, blob)
        done = self.run(["sh", "-c", "mkdir -p '%s' && tar -xf '%s' -C '%s' && rm -f '%s'"
                         % (remote_dir, staged, remote_dir, staged)], timeout=900)
        if not done.ok:
            raise SandboxError("could not unpack into the sandbox: %s" % done.tail())

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
        try:
            handle = self._sandbox.commands.run(
                " ".join(_quote(part) for part in argv),
                cwd=workdir or "/home/user", envs=dict(env or {}),
                timeout=int(timeout))
        except Exception as exc:                              # noqa: BLE001 -- the SDK's own errors
            code = getattr(exc, "exit_code", None)
            return Result(1 if code is None else int(code),
                          _text(getattr(exc, "stdout", "")),
                          _text(getattr(exc, "stderr", "")) or str(exc)[-2000:],
                          time.perf_counter() - started)
        return Result(getattr(handle, "exit_code", 0), _text(getattr(handle, "stdout", "")),
                      _text(getattr(handle, "stderr", "")), time.perf_counter() - started)

    def pull(self, remote_dir: str, local_dir: str) -> None:
        staged = "/tmp/frf-pull-%s.tar" % uuid.uuid4().hex[:8]
        done = self.run(["sh", "-c", "tar -cf '%s' -C '%s' ." % (staged, remote_dir)], timeout=900)
        if not done.ok:
            raise SandboxError("could not pack the sandbox directory %s: %s"
                               % (remote_dir, done.tail()))
        # `bytes` comes back as a bytearray from this SDK, and tarfile wants something it can wrap
        # in a BytesIO -- so it is normalised here rather than at the one place that noticed.
        blob = bytes(self._sandbox.files.read(staged, format="bytes"))
        self.run(["rm", "-f", staged], timeout=60)
        _untar_bytes(blob, local_dir)


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
    return tempfile.mkdtemp(prefix="frf-stage-")
