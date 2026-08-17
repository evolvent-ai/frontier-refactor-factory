"""Line coverage for a Python subject, using the interpreter's own tracer.

`sys.settrace` rather than a third-party tool, for one reason worth stating: this factory has no
runtime dependencies, and a coverage measurement is not worth acquiring one. The interpreter reports
every line it executes, which is exactly the measurement wanted, and the whole backend is the few
lines below.

MEASURED IN A SEPARATE PROCESS, ALWAYS. The tracer has to be armed before the subject is imported,
and it makes every line of everything slower -- so it must not be attached to a process that is
also being timed, and it must not outlive the measurement. Running it in a child keeps both
guarantees without the caller having to remember either.

WHAT IS COUNTED. Executable lines of the subject's own files, and nothing else: the standard library
and installed packages are not what a solver is being asked to reimplement, so including them would
put a denominator in the report that no corpus could ever move.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from ...core.adequacy import Reach

# How many dark regions to name. Enough to aim a repair at, few enough that a provenance file stays
# readable -- the fraction is the summary and this is the actionable part.
DARK_LIMIT = 20

# The child process. Written out rather than passed as `-c` so that a traceback from it names a file
# and a line, which is the difference between a debuggable failure and a wall of quoted source.
_HARNESS = '''\
import json, os, sys, trace

target, probes_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
root = os.path.dirname(os.path.abspath(target))

# `trace` counts lines rather than sampling, which is what makes the answer exact. The cost is that
# everything runs slowly, which is why nothing timed shares this process.
tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])

namespace = {"__name__": "__subject__", "__file__": target}
source = open(target).read()
compiled = compile(source, target, "exec")

probes = json.load(open(probes_path))
symbol = sys.argv[4] if len(sys.argv) > 4 else "entry"
def drive():
    tracer.runctx(compiled, namespace, namespace)
    # THE SAME SYMBOL AND THE SAME CALLING CONVENTION AS THE SHIM. Measuring coverage by calling
    # the subject differently from the way it is graded would report the reach of a program nobody
    # runs -- and the disagreement is silent, because both halves work perfectly on their own.
    entry = namespace.get(symbol)
    if entry is None:
        return
    for args in probes:
        try:
            tracer.runfunc(entry, *args)
        except Exception:
            # A refusal is a path through the subject like any other, and its lines count.
            pass

drive()

counts = tracer.results().counts
executed = {}
for (filename, lineno) in counts:
    if os.path.abspath(filename).startswith(root):
        executed.setdefault(os.path.abspath(filename), set()).add(lineno)

report = {}
for filename, lines in executed.items():
    report[os.path.relpath(filename, root)] = sorted(lines)
json.dump(report, open(out_path, "w"))
'''


def _executable_lines(path: str) -> set:
    """Which lines of a file could execute at all.

    The denominator has to exclude blanks, comments and docstrings, or a heavily documented subject
    reports low coverage for being well commented. Derived from the compiled code object rather than
    by parsing text, because the interpreter's opinion about which lines are executable is the one
    the tracer will agree with.
    """
    try:
        compiled = compile(open(path, encoding="utf-8").read(), path, "exec")
    except (OSError, SyntaxError):
        return set()

    lines, pending = set(), [compiled]
    while pending:
        code = pending.pop()
        lines.update(lineno for lineno in _line_numbers(code) if lineno)
        pending.extend(c for c in code.co_consts if hasattr(c, "co_code"))
    return lines


def _line_numbers(code) -> list:
    """Every line the interpreter attributes to this code object.

    `co_lines()` yields (start, end, line) triples and the line may be None for bytecode that
    belongs to no source line -- both facts are easy to get wrong, and getting them wrong inflates
    the denominator with lines that can never be reached.
    """
    try:
        return [entry[2] for entry in code.co_lines()]      # 3.10+
    except AttributeError:                                  # pragma: no cover -- older interpreters
        return [code.co_firstlineno]


class PythonTrace:
    """Line coverage via the interpreter's own tracer, measured in a child process."""

    name = "python-trace"

    def measure(self, spec, probes) -> Reach:
        """-> what the corpus reached of the subject's own lines.

        A failure to measure returns an UNMEASURED reach rather than a zero. The two mean opposite
        things: unmeasured is an absence the report states, and zero is a broken tracer that fails
        adequacy. Reporting a failed measurement as zero would refuse tasks for our own bug.
        """
        target = self._subject_path(spec)
        if not target or not os.path.exists(target):
            return Reach(backend=self.name)

        with tempfile.TemporaryDirectory() as work:
            harness = os.path.join(work, "measure_coverage.py")
            probes_path = os.path.join(work, "probes.json")
            out_path = os.path.join(work, "reached.json")
            with open(harness, "w") as handle:
                handle.write(_HARNESS)
            with open(probes_path, "w") as handle:
                json.dump(list(self._as_args(probes)), handle)

            done = subprocess.run([sys.executable, harness, target, probes_path, out_path,
                                   self._symbol_of(spec)],
                                  capture_output=True, text=True, timeout=600)
            if done.returncode != 0 or not os.path.exists(out_path):
                return Reach(backend=self.name)
            reached_by_file = json.load(open(out_path))

        root = os.path.dirname(os.path.abspath(target))
        total = reached = 0
        dark = []
        for relative in sorted(set(reached_by_file) | {os.path.basename(target)}):
            path = os.path.join(root, relative)
            executable = _executable_lines(path)
            if not executable:
                continue
            hit = set(reached_by_file.get(relative, ())) & executable
            total += len(executable)
            reached += len(hit)
            missed = len(executable) - len(hit)
            if missed:
                dark.append((missed, relative))

        ranked = tuple("%s (%d line(s) unreached)" % (name, missed)
                       for missed, name in sorted(dark, reverse=True)[:DARK_LIMIT])
        return Reach(reached=reached, total=total, dark=ranked, backend=self.name)

    @staticmethod
    def _symbol_of(spec) -> str:
        """Which function to drive. Read from the spec, exactly as the shim reads it."""
        return getattr(spec, "entry", "") or "entry"

    @staticmethod
    def _subject_path(spec) -> str:
        """Where the subject's source is. Read from the spec rather than guessed."""
        return (spec.environment or {}).get("subject_path", "")

    @staticmethod
    def _as_args(probes) -> list:
        """Probes come as {id: args} from the call seam and as a sequence elsewhere."""
        if isinstance(probes, dict):
            return list(probes.values())
        return [p for p in (probes or ())]
