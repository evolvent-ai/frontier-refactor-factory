"""Running a command and recording what the process did.

A step is one command in a prepared directory. What it leaves behind is four things -- exit code,
stdout, stderr, and the state of the directory afterwards -- and this module's whole job is to
capture them in a form two runs of the same program can be compared in.

WHY THE WORKSPACE IS ALWAYS FRESH AND ALWAYS REMOVED. A corpus is executed thousands of times, so a
leaked directory is a filled disk rather than a debugging convenience -- and a REUSED one is worse:
step N would observe what step N-1 left, making the recording depend on the order the corpus
happened to run in, which no submission can reproduce.

WHY PATHS ARE SCRUBBED. The temporary directory has a different name every run. Left in the output
it varies between runs and the freeze masks the line, so a program that prints a path loses a graded
line for a reason that has nothing to do with its behaviour. Replacing the workspace root with a
fixed token keeps the line gradeable and removes a difference no solver could ever match.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from .observation import Observation, Stream

# What the workspace path becomes in a recorded observation. Anything a program prints that contains
# the real directory is rewritten to this, so the recording is about the program and not about where
# it happened to run.
WORKSPACE_TOKEN = "<workspace>"

DEFAULT_STEP_TIMEOUT = 300.0


@dataclass(frozen=True)
class Step:
    """One command, and where it runs relative to the workspace root."""

    argv: list
    cwd: str = "."
    stdin: str | None = None

    def to_json(self) -> dict:
        return {"argv": list(self.argv), "cwd": self.cwd, "stdin": self.stdin}

    @classmethod
    def from_json(cls, data: dict) -> "Step":
        return cls(argv=list(data["argv"]), cwd=data.get("cwd", "."), stdin=data.get("stdin"))


@dataclass(frozen=True)
class Scenario:
    """A sequence of commands run in one prepared directory.

    Sequences rather than single commands because state is part of the behaviour: `init` then `add`
    then `commit` tests something no one of them tests alone, and a program that gets the third
    wrong after getting the first two right is exactly the failure worth catching.
    """

    probe_id: str
    steps: list = field(default_factory=list)
    fixture: str | None = None                  # a tarball unpacked before step 0
    environment: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"probe_id": self.probe_id, "fixture": self.fixture,
                "environment": self.environment, "steps": [s.to_json() for s in self.steps]}

    @classmethod
    def from_json(cls, data: dict) -> "Scenario":
        return cls(probe_id=data["probe_id"],
                   steps=[Step.from_json(s) for s in data["steps"]],
                   fixture=data.get("fixture"), environment=dict(data.get("environment") or {}))


def _tree_lines(root: str, exclude: tuple = ()) -> list:
    """The directory, as sorted lines. One line per file: mode, size, path.

    Sorted because the order a filesystem lists entries in is not behaviour, and grading it would
    fail correct submissions on a different filesystem. Content is not hashed here -- the digest of
    the whole listing is what gets compared, and including per-file hashes would make a change in
    one file look like a change in the whole tree.
    """
    lines = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in exclude)
        for name in sorted(files):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, root)
            if any(part in exclude for part in relative.split(os.sep)):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            executable = "x" if stat.st_mode & 0o111 else "-"
            lines.append("%s %d %s" % (executable, stat.st_size, relative))
    return sorted(lines)


def _scrub(text: str, workspace: str) -> str:
    return text.replace(workspace, WORKSPACE_TOKEN)


def run_scenario(scenario: Scenario, program: list, *, fixtures_dir: str | None = None,
                 exclude: tuple = (), timeout: float = DEFAULT_STEP_TIMEOUT) -> list:
    """Run every step in a fresh workspace. -> one Observation per step.

    A step that fails does NOT abort the scenario. "The program errored here and recovered there" is
    behaviour, and a reimplementation has to reproduce it -- stopping at the first non-zero exit
    would silently stop grading everything after it.
    """
    root = tempfile.mkdtemp(prefix="frf-scenario-")
    workspace = os.path.join(root, "workspace")
    os.makedirs(workspace, exist_ok=True)
    # READABLE BY THE ACCOUNT THE SUBJECT RUNS AS. `mkdtemp` creates 0700, which is right for a
    # temporary directory and wrong here: the whole point of the isolation wrapper is that the
    # subject runs as somebody else, and that somebody then cannot read its own inputs. The failure
    # is "permission denied" on a graded channel, which looks like the program rejecting the file.
    # The directory is inside a per-scenario temp root that is removed afterwards, so opening it is
    # not a wider exposure than the run itself.
    os.chmod(root, 0o755)
    os.chmod(workspace, 0o755)
    observations = []
    try:
        if scenario.fixture and fixtures_dir:
            shutil.unpack_archive(os.path.join(fixtures_dir, scenario.fixture), workspace)

        environment = dict(os.environ)
        # WHAT A SHELL SOURCES AT STARTUP IS NOT THE SUBJECT'S BEHAVIOUR. `sh` reads $ENV and bash
        # reads $BASH_ENV before running anything, so on a host that points them at a system profile
        # the profile's output lands on a GRADED channel: this machine sets ENV=/etc/shinit_v2,
        # which runs dpkg, which warns about a file it cannot read. Frozen, that warning becomes
        # part of the expectation, and a submission is then judged on whether it reproduces our
        # harness's noise on our machine. Removed here rather than filtered from the output,
        # because a channel must record what the subject did.
        environment.pop("ENV", None)
        environment.pop("BASH_ENV", None)
        environment.update(scenario.environment)
        for step in scenario.steps:
            cwd = os.path.normpath(os.path.join(workspace, step.cwd))
            os.makedirs(cwd, exist_ok=True)
            # THE WHOLE-TOKEN CASE FIRST, and the order is the bug this comment exists for. Doing
            # the textual replacement first rewrites a bare `{PROGRAM}` into `program[0]`, so the
            # test below never matches and the rest of `program` is dropped -- which is invisible
            # while the program is one word, and catastrophic once it is not. Wrapping the two sides
            # for isolation makes it several words: `setpriv --reuid nobody -- sh -c ... -- prog`.
            # Every scenario then ran `setpriv` with the step's arguments instead of the program,
            # produced identical output, and the corpus scored a do-nothing submission at 100%.
            if step.argv and step.argv[0] == "{PROGRAM}":
                argv = list(program) + list(step.argv[1:])
            else:
                # An embedded `{PROGRAM}`, as in `sh -c '{PROGRAM} --help'`. Only the executable can
                # be substituted textually, so a multi-word program is joined back into one string.
                joined = " ".join(program)
                argv = [part.replace("{PROGRAM}", joined) if isinstance(part, str) else part
                        for part in step.argv]

            try:
                done = subprocess.run(argv, cwd=cwd, env=environment, input=step.stdin,
                                      capture_output=True, text=True, timeout=timeout)
                code, out, err = done.returncode, done.stdout, done.stderr
            except subprocess.TimeoutExpired as exc:
                code, out, err = -1, exc.stdout or "", (exc.stderr or "") + "\n[timed out]"
            except OSError as exc:
                # The program is not there. That is a real, gradeable outcome for a candidate that
                # failed to build, so it is an observation rather than an exception.
                code, out, err = 127, "", "could not execute: %s" % exc

            observations.append(Observation(
                exit_code=code,
                stdout=Stream.of(_scrub(out, workspace)),
                stderr=Stream.of(_scrub(err, workspace)),
                tree=Stream(tuple(_tree_lines(workspace, exclude)))))
        return observations
    finally:
        shutil.rmtree(root, ignore_errors=True)


def time_scenario(scenario: Scenario, program: list, *, repeats: int = 3, **kwargs) -> float:
    """Wall-clock cost of one scenario, taken as the MINIMUM over repeats.

    Minimum rather than mean because interference on a shared machine is one-sided: something else
    running can only ever make this slower. The fastest observation is therefore the closest estimate
    of what the program costs, and the least sensitive to a neighbour.
    """
    import time

    best = float("inf")
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        run_scenario(scenario, program, **kwargs)
        best = min(best, time.perf_counter() - started)
    return best
