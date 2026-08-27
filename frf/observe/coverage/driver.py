"""Running a subject over the corpus so that something can watch which lines it executes.

Every coverage backend needs a subject driven over the corpus, with whatever the language's
instrumentation is watching. Only the instrumentation part differs per language, so the shared
machinery lives here.

NOTHING HERE RAISES. Coverage is a report and not a gate, so a compiler that is not installed, a
build that fails, a subject that will not start -- all of them return "not measured" and the task
ships with one number fewer. The single thing that must not happen is returning a measured ZERO for
a measurement that did not occur, because that is a broken-tracer verdict and it fails adequacy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from ...core import scratch

# Longest the instrumented subject may take over the whole corpus. Instrumentation makes everything
# slower -- a tracer can be an order of magnitude -- so this is far more generous than a timing run.
TIMEOUT = 900.0


def probe_lines(probes) -> list:
    """The corpus as request lines the subject runner will answer.

    Probes arrive as `{id: args}` from the call seam and as a plain sequence elsewhere, and both
    shapes reach here. Normalising in one place beats each backend remembering which it was given.
    """
    if isinstance(probes, dict):
        drawn = list(probes.values())
    else:
        drawn = list(probes or ())
    return [json.dumps({"id": i, "op": "run", "call": "entry", "args": args},
                       separators=(",", ":")) + "\n"
            for i, args in enumerate(drawn)]


def subject_path(spec) -> str:
    """Where the subject's source is. Read from the spec rather than guessed."""
    return (spec.environment or {}).get("subject_path", "")


class Run:
    """One instrumented run of a subject, in a directory that is cleaned up afterwards.

    A context manager because every backend needs the same teardown and because the coverage data
    a tool leaves behind is often several files in a directory nobody would think to remove.
    """

    def __init__(self, spec, probes, *, language: str, extra_env: dict | None = None,
                 build_override=None) -> None:
        self.spec = spec
        self.probes = probes
        self.language = language
        self.extra_env = dict(extra_env or {})
        # How to compile, when instrumentation needs different flags than a plain build. A callable
        # rather than a flag because "add --coverage" is not a general rule: Go replaces the whole
        # command, and C has to keep its notes file beside its data file.
        self.build_override = build_override
        self.work = ""
        self.ok = False
        self.source = ""

    def __enter__(self) -> "Run":
        self.work = scratch.mkdtemp(prefix="frf-coverage-")
        self.ok = self._drive()
        return self

    def __exit__(self, *_) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def path(self, *parts: str) -> str:
        return os.path.join(self.work, *parts)

    def _drive(self) -> bool:
        """Materialise, build, and feed the corpus in. -> whether it ran at all.

        False for every kind of not-having-happened, and the caller turns that into an unmeasured
        reach. Distinguishing "the compiler is missing" from "the subject crashed" would be useful
        for a repair loop and is deliberately not done here: coverage does not gate anything, so a
        detailed diagnosis would be a detailed diagnosis of something nobody is waiting on.
        """
        target = subject_path(self.spec)
        if not target or not os.path.exists(target):
            return False
        build, argv = self._build_argv(target)
        if build is None:
            return False
        try:
            self.source = open(target, encoding="utf-8", errors="replace").read()
        except OSError:
            return False

        if self.build_override is not None:
            build, argv = self.build_override(self.work, build, argv)

        environment = dict(os.environ)
        environment.update(self.extra_env)
        lines = probe_lines(self.probes)
        try:
            for command in build:
                done = subprocess.run(command, cwd=self.work, env=environment,
                                      capture_output=True, text=True, timeout=TIMEOUT)
                if done.returncode != 0:
                    return False
            served = subprocess.run(argv, cwd=self.work, env=environment,
                                    input="".join(lines),
                                    capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return False

        if lines and not served.stdout.strip():
            return False
        return True

    def _build_argv(self, target: str) -> tuple:
        """How to build and run the subject for coverage. -> (build_commands, argv) or (None, None).

        Subclasses override this to inject instrumentation flags. The base implementation returns
        (None, None) so that a language with no coverage backend gracefully returns unmeasured.
        """
        return None, None

    def subject_name(self) -> str:
        """What the subject's file is called in the workspace, for matching a tool's report."""
        return os.path.basename(subject_path(self.spec))
