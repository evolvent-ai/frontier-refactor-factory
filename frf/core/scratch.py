"""Where the HOST stages its temporary working directories.

A checkout, a mutant build room, a per-scenario workspace and a pulled result snapshot are all
staged on the host before or after the subject runs. They are large -- a repository checkout is
hundreds of megabytes -- and there are thousands of them across a batch.

WHY THIS IS NOT `tempfile.mkdtemp`. The default lands on the system temp directory, which is on the
root filesystem on a normal machine. That filesystem holds the OS and is sized for the OS, so a
batch that stages a few hundred checkouts fills it, and the failure is not reported as "out of
space" by the thing that ran out: a freeze several layers up dies with `OSError: [Errno 28]` on a
graded channel, which reads like the subject misbehaving. Meanwhile the volume the project actually
lives on may have hundreds of gigabytes free and go unused. So the base directory is chosen
deliberately, next to the project's other bulk output, rather than inherited.

WHY `TMPDIR` DOES NOT WIN. Honouring it would undo the choice above whenever the surrounding shell
happens to export the system default, which is the common case and is not an instruction. An
explicit `FRF_SCRATCH_DIR` is an instruction, and is honoured; `TMPDIR` is consulted only when there
is no writable project root to put the directory beside.

This module stages directories and does not know what is put in them: it names no channel, freezes
nothing and grades nothing, so it stays in `core/`.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time


ENV_VAR = "FRF_SCRATCH_DIR"

# Every directory this module hands out starts with this, so a sweep can tell what it is allowed to
# remove from what somebody else left in the same place.
PREFIX = "frf-"

# A leftover is removed only once it is old enough that no live run could still own it. Directories
# are handed out and then held for as long as a candidate takes, so the threshold is a bound on that
# rather than a guess: a batch that has been running for a day has bigger problems than disk.
DEFAULT_MAX_AGE_HOURS = 24.0


def _project_root() -> str:
    """The checkout this package is running from, or "" when it is installed elsewhere."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../frf/core -> ...
    root = os.path.dirname(here)
    return root if os.path.exists(os.path.join(root, "pyproject.toml")) else ""


def base() -> str:
    """The directory temporary working directories are created inside, created if missing."""
    chosen = (os.environ.get(ENV_VAR) or "").strip()
    if not chosen:
        root = _project_root()
        # `work/` is where the project already keeps bulk generated material and is already
        # ignored by git, so scratch belongs beside it rather than in a new top-level name.
        chosen = os.path.join(root, "work", "scratch") if root else os.path.join(
            tempfile.gettempdir(), "frf-scratch")
    os.makedirs(chosen, exist_ok=True)
    # A subject may run as another account, and one of its inputs is staged in a directory below
    # this one.  `makedirs` honours the umask, which can leave this level unsearchable for that
    # account -- the resulting "permission denied" surfaces as the program rejecting its input.
    # Only traversal is opened here; the per-run directory below keeps mkdtemp's own 0700.
    try:
        os.chmod(chosen, 0o755)
    except OSError:
        pass
    return chosen


def mkdtemp(prefix: str = PREFIX) -> str:
    """`tempfile.mkdtemp` with this project's base directory and 0700, as before."""
    return tempfile.mkdtemp(prefix=prefix, dir=base())


def temporary_directory(prefix: str = PREFIX):
    """`tempfile.TemporaryDirectory` rooted here, for the callers that clean up by scope."""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=base())


def sweep(*, max_age_hours: float = DEFAULT_MAX_AGE_HOURS, now: float | None = None) -> int:
    """Remove this project's abandoned directories, returning how many went.

    A run that dies -- a killed batch, a gateway timeout, a full disk -- does not get to run its own
    cleanup, so leftovers accumulate silently until the filesystem is full. Age is the only signal
    available for whether anybody still owns a directory, so a generous threshold is used and
    anything younger is left strictly alone.
    """
    root = base()
    cutoff = (now if now is not None else time.time()) - max_age_hours * 3600.0
    removed = 0
    for name in os.listdir(root):
        if not name.startswith(PREFIX):
            continue                       # not ours to delete, whatever it is
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path) or os.path.islink(path):
                continue
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            continue                       # vanished underneath us, or unreadable: leave it
        try:
            shutil.rmtree(path)
        except OSError:
            continue                       # in use or not ours to remove; the next sweep retries
        removed += 1
    return removed


__all__ = ["ENV_VAR", "PREFIX", "DEFAULT_MAX_AGE_HOURS", "base", "mkdtemp",
           "temporary_directory", "sweep"]
