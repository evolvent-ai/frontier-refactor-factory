"""Somewhere to build and run things that is not this machine.

Two reasons, and only the second is about safety.

THE KEY DESCRIBES WHATEVER RAN IT. An Expectation records a reference's real behaviour, so
whichever machine produced it becomes part of what it means. Freeze against this host's
toolchain, ship an image without it, and the task describes a program the solver never receives.
The fix is not care -- it is to freeze inside the image the task ships with, which makes the whole
class of mismatch impossible instead of enumerating its members.

BUILDING IN THE IMAGE IS ALSO THE CHEAPEST CHECK ON IT. A subject whose environment cannot build
fails in the first minute rather than at packaging time, or never.

The interface is deliberately small -- put a directory in, run a command, take a directory out --
because everything above it is written against the interface and must not learn which backend it
got. `find()` picks one and says what it picked; a caller that wants a specific backend asks for it
by name and gets a clear failure if it is unavailable, rather than a silent downgrade to something
that measures differently.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from . import credentials


class SandboxError(RuntimeError):
    """No usable sandbox, or one that failed in a way the caller cannot act on."""


@dataclass(frozen=True)
class Result:
    """What one command did. `ok` is exit-status zero; the streams are captured whole."""

    exit_code: int
    stdout: str
    stderr: str
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def tail(self, limit: int = 2000) -> str:
        """The end of what it said -- which is where a build failure explains itself."""
        joined = (self.stdout + self.stderr).strip()
        return joined[-limit:] if len(joined) > limit else joined


class Backend(Protocol):
    """What every sandbox must do. Nothing above this layer may need more than these."""

    name: str

    def push(self, local_dir: str, remote_dir: str) -> None: ...

    def run(self, argv: list[str], *, workdir: str | None = None,
            env: dict | None = None, timeout: float = 3600.0) -> Result: ...

    def pull(self, remote_dir: str, local_dir: str) -> None: ...

    def close(self) -> None: ...


@dataclass
class LocalProcess:
    """A "sandbox" that is this machine. For developing the pipeline, never for freezing a key.

    It exists because the alternative during development is mocking a container, and a mocked
    container tests the mock. It is named honestly so that no caller can reach for it by accident
    and quietly produce an Expectation describing this host.
    """

    root: str
    name: str = "local-process"

    def push(self, local_dir: str, remote_dir: str) -> None:
        shutil.copytree(local_dir, remote_dir, dirs_exist_ok=True)

    def run(self, argv: list[str], *, workdir: str | None = None,
            env: dict | None = None, timeout: float = 3600.0) -> Result:
        import time
        started = time.perf_counter()
        try:
            done = subprocess.run(argv, cwd=workdir or self.root, env=env,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return Result(-1, exc.stdout or "", (exc.stderr or "") + "\n[timed out]",
                          time.perf_counter() - started)
        except OSError as exc:
            # A command that could not start is not a command that failed. Callers route on this.
            return Result(127, "", "could not execute %r: %s" % (argv[:2], exc),
                          time.perf_counter() - started)
        return Result(done.returncode, done.stdout, done.stderr, time.perf_counter() - started)

    def pull(self, remote_dir: str, local_dir: str) -> None:
        shutil.copytree(remote_dir, local_dir, dirs_exist_ok=True)

    def close(self) -> None:
        return None


def available() -> dict[str, bool]:
    """Which backends this machine could actually use, checked rather than assumed.

    Reported as a mapping so a caller can say WHY it fell back, and so the answer appears in a log
    instead of being inferred from a stack trace three stages later.
    """
    docker = bool(shutil.which("docker"))
    if docker:
        try:
            docker = subprocess.run(["docker", "info"], capture_output=True,
                                    timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            docker = False
    return {"docker": docker,
            "remote": bool(credentials.get("E2B_API_KEY")),
            "local-process": True}


def find(prefer: str | None = None) -> Backend:
    """Pick a backend. -> the one chosen, having said so.

    `prefer` is honoured or refused, never silently substituted: a caller that asked to freeze in a
    container and got this process instead would produce an Expectation describing this host, and
    would have no way to know.
    """
    have = available()
    if prefer:
        if not have.get(prefer):
            raise SandboxError(
                "backend %r is not available here (available: %s). Refusing to substitute another: "
                "a key frozen somewhere other than where it was asked for describes the wrong "
                "machine." % (prefer, ", ".join(k for k, v in have.items() if v)))
        return _build(prefer)
    for candidate in ("docker", "remote"):
        if have.get(candidate):
            return _build(candidate)
    raise SandboxError(
        "no container backend: docker is unreachable and no remote sandbox key is set. Freezing "
        "would describe this host rather than the image the task ships with. Set E2B_API_KEY, or "
        "start a docker daemon, or ask for 'local-process' explicitly if this is a dry run.")


def _build(name: str) -> Backend:
    if name == "local-process":
        import tempfile
        return LocalProcess(root=tempfile.mkdtemp(prefix="frf-local-"))
    # The container backends are written against the same Protocol and arrive with the scale that
    # first needs one. Failing here names what is missing rather than raising an AttributeError
    # from somewhere that has forgotten it asked.
    raise SandboxError("backend %r is not implemented yet" % name)
