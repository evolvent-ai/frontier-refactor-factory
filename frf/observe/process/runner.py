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
import uuid
from dataclasses import dataclass, field

from ...core import scratch
from .observation import Observation, Stream

# What the workspace path becomes in a recorded observation. Anything a program prints that contains
# the real directory is rewritten to this, so the recording is about the program and not about where
# it happened to run.
WORKSPACE_TOKEN = "<workspace>"

# THE ENVIRONMENT A SUBJECT IS OBSERVED IN. DECLARED, NEVER INHERITED.
#
# Three places run the subject and all three must agree: the local runner, the remote runner that
# every sandboxed freeze uses, and the verifier shipped inside the task. Each of them built its
# environment from `dict(os.environ)` -- and those are three DIFFERENT environments. The freeze
# pushed the factory host's 54 variables into the sandbox; the shipped verifier read the delivered
# image's own. So the reference was observed under one environment and graded under another.
#
# WHAT THAT COST, and the signature is unmistakable. Of 150 tasks refused at
# `does-not-reproduce-in-its-own-image`, 145 matched EXACTLY one or two of the four graded channels:
# 76 at 2/4, 69 at 1/4. Material variation does not land on quarter boundaries -- a whole channel
# failing on every scenario is an environment difference. `TERM=xterm-256color` is enough on its own:
# a program that colours its output when a terminal is present and does not otherwise diverges on
# stdout AND stderr, on every scenario, while exit code and file tree still match.
#
# `ENV` and `BASH_ENV` were removed from the inherited copy for exactly this reason -- a profile that
# ran dpkg and wrote a warning to a graded channel. That fix named two variables. INHERITING AT ALL
# is the defect, and this makes the class impossible instead of enumerating its members.
#
# Set rather than merely unset, because "absent here, present there" is the bug. A value that is
# written down is one a reader of the shipped task can see.
SUBJECT_ENV = {
    # Locale decides number formatting, collation and case folding. Left to the machine, sorted
    # output differs between the sandbox and the image.
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    # Timestamps a program prints itself. Not the same as an unstable clock, which masking handles.
    "TZ": "UTC",
    # No terminal, so no colour and no width-dependent wrapping. The four below say the same thing
    # to different libraries; a program obeys whichever one it knows.
    "TERM": "dumb",
    "COLUMNS": "80",
    "LINES": "24",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "CLICOLOR_FORCE": "0",
    "FORCE_COLOR": "0",
    # Python subjects otherwise pick an encoding from the locale, which differs per image.
    "PYTHONIOENCODING": "utf-8",
}

# The only variables taken from the surroundings, and they must come from WHERE THE PROGRAM RUNS
# rather than from this process. A host `PATH` of `/root/.vscode-server/...` means nothing inside a
# sandbox, and forwarding it replaces the one that would have found the program.
INHERITED_ENV = ("PATH", "HOME")


def subject_environment(scenario_env: dict | None = None, *, inherit_from: dict | None = None) -> dict:
    """The environment for one observation. -> a fresh dict.

    `inherit_from` is the environment of the machine that will RUN the subject -- `os.environ` for
    the local runner and for the shipped verifier, and NOTHING for the remote runner, where the
    sandbox already has its own and the SDK adds these on top of it.
    """
    environment = {}
    for name in INHERITED_ENV:
        value = (inherit_from or {}).get(name)
        if value:
            environment[name] = value
    environment.update(SUBJECT_ENV)
    # The scenario's own declarations win: they are part of what was harvested and are recorded in
    # the shipped corpus, so a reader can see them.
    environment.update(scenario_env or {})
    return environment

DEFAULT_STEP_TIMEOUT = 300.0
PROGRAM_TOKEN = "{PROGRAM}"


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


from .snapshot import tree_lines as _tree_lines


def _make_output_dirs(workspace: str, argv: list) -> None:
    """Create the directories a maintainer's command assumes already exist.

    A harvested invocation carries the repository's layout with it -- `-o ./src/parser.js` is
    written by somebody whose `src/` exists. The scenario workspace holds only the fixture, so the
    write fails, the program exits non-zero with nothing on stdout, and the candidate is refused for
    having done nothing. That refusal is ours.

    Only inside the workspace, and only for tokens that name a directory: this creates somewhere to
    write, never the file itself, so a program that was going to read a missing input still fails
    exactly as it would.
    """
    for token in argv:
        token = str(token)
        if not token or token.startswith("-") or "/" not in token:
            continue
        parent = os.path.dirname(os.path.normpath(os.path.join(workspace, token.lstrip("./"))))
        if os.path.commonpath([os.path.abspath(parent), os.path.abspath(workspace)]) == \
                os.path.abspath(workspace):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass


def _scrub(text: str, workspace: str) -> str:
    return text.replace(workspace, WORKSPACE_TOKEN)


def run_scenario(scenario: Scenario, program: list, *, fixtures_dir: str | None = None,
                 exclude: tuple = (), timeout: float = DEFAULT_STEP_TIMEOUT,
                 backend=None, remote_program: list | None = None,
                 remote_fixtures: str | None = None) -> list:
    """Run every step in a fresh workspace. -> one Observation per step.

    A step that fails does NOT abort the scenario. "The program errored here and recovered there" is
    behaviour, and a reimplementation has to reproduce it -- stopping at the first non-zero exit
    would silently stop grading everything after it.
    """
    if backend is not None and getattr(backend, "name", "") not in ("local-process", ""):
        return _run_remote_scenario(scenario, program, backend=backend,
                                    remote_program=remote_program or program,
                                    remote_fixtures=remote_fixtures,
                                    exclude=exclude, timeout=timeout)
    root = scratch.mkdtemp(prefix="frf-scenario-")
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

        # DECLARED, NOT INHERITED -- see SUBJECT_ENV. This runs on the factory host, so `PATH` and
        # `HOME` come from here; everything else that could reach a graded channel is written down.
        # `ENV` and `BASH_ENV` are gone by construction rather than by being popped: they are not in
        # SUBJECT_ENV, so a shell started here sources nothing.
        environment = subject_environment(scenario.environment, inherit_from=os.environ)
        for step in scenario.steps:
            cwd = os.path.normpath(os.path.join(workspace, step.cwd))
            os.makedirs(cwd, exist_ok=True)
            _make_output_dirs(workspace, step.argv)
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


def run_remote_many(scenarios: list[Scenario], *, backend, remote_program: list,
                    remote_fixtures: str | None, exclude: tuple = (),
                    timeout: float = DEFAULT_STEP_TIMEOUT) -> dict:
    """Run bounded batches remotely, returning observations instead of entire file trees."""
    if len(scenarios) > 8:
        merged = {}
        for start in range(0, len(scenarios), 8):
            merged.update(run_remote_many(scenarios[start:start + 8], backend=backend,
                                          remote_program=remote_program,
                                          remote_fixtures=remote_fixtures, exclude=exclude,
                                          timeout=timeout))
        return merged

    import json
    from pathlib import Path
    from ...core.sandbox import SandboxError
    from .remote_driver import standalone_source
    remote_root = "/tmp/frf-corpus-%s" % uuid.uuid4().hex[:12]
    with scratch.temporary_directory() as local:
        request = {'root': remote_root, 'scenarios': [s.to_json() for s in scenarios],
                   'program': list(remote_program), 'fixtures': remote_fixtures,
                   'exclude': list(exclude), 'timeout': timeout, 'environment': subject_environment()}
        Path(local, 'driver.py').write_text(standalone_source())
        Path(local, 'request.json').write_text(json.dumps(request))
        try:
            backend.push(local, remote_root)
            budget = 30 + (timeout + 5) * max(1, sum(len(s.steps) for s in scenarios))
            done = backend.run(['python3', remote_root + '/driver.py', remote_root + '/request.json',
                                remote_root + '/observations.json'], timeout=budget)
            if not done.ok:
                raise SandboxError('remote observation driver failed: %s' % done.tail())
            report = backend.run(['cat', remote_root + '/observations.json'], timeout=30)
            if not report.ok:
                raise SandboxError('remote observations could not be read: %s' % report.tail())
            try:
                raw = json.loads(report.stdout)
                answer = {}
                for scenario in scenarios:
                    items = raw[scenario.probe_id]
                    if len(items) != len(scenario.steps):
                        raise ValueError('incomplete scenario report')
                    answer[scenario.probe_id] = [
                        Observation(item['exit_code'], Stream.of(item['stdout']),
                                    Stream.of(item['stderr']), Stream(tuple(item['tree'])))
                        for item in items]
                return answer
            except (ValueError, KeyError, TypeError) as error:
                raise SandboxError('invalid remote observation report: %s' % error) from error
        finally:
            backend.run(['rm', '-rf', remote_root], timeout=60)


def _run_remote_scenario(scenario: Scenario, program: list, *, backend,
                         remote_program: list, remote_fixtures: str | None,
                         exclude: tuple, timeout: float) -> list:
    """Use the same execution and serialization as the batched remote path."""
    return run_remote_many([scenario], backend=backend, remote_program=remote_program,
                           remote_fixtures=remote_fixtures, exclude=exclude,
                           timeout=timeout)[scenario.probe_id]
