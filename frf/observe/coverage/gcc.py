"""Line coverage for a C or C++ subject, through gcc's own arc profiling.

`--coverage` on the compile and the link, then `gcov` to read what the run left behind. No third
party: this is the instrumentation the compiler already has, and it reports both the lines that ran
AND the lines that did not, which makes the denominator real rather than a guess about which lines
were code.

THE TRAP THIS FILE EXISTS TO AVOID, and it has cost days elsewhere. gcov needs two files: the notes
(`.gcno`) written at compile time and the data (`.gcda`) written when the program exits. They must
be found together, and gcc names them after the OBJECT it compiled -- so building `serve.c` and
`subject.c` into a binary called `sc` produces `sc-subject.gcno`, not `subject.gcno`. Point gcov at
the source file name and it answers "cannot open notes file", produces an empty report, and the
whole thing looks exactly like a language with no coverage backend. A zero that means "I looked in
the wrong place" is the single most misleading number this package could produce, so the stem is
computed here rather than assumed, and a report that names no file returns UNMEASURED.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import subprocess

from . import driver, spans

# The compiler flags that turn on arc profiling. On both the compile and the link, which is easy to
# get half right: without it on the link the program has no runtime to write the .gcda out.
COVERAGE_FLAGS = ("--coverage", "-O0")


class GccCoverage:
    """gcc/g++ arc profiling, read through `gcov -i`."""

    def __init__(self, language: str = "c") -> None:
        self.language = language
        self.name = "gcov"

    def measure(self, spec, probes) -> spans.Reach:
        with driver.Run(spec, probes, language=self.language,
                        build_override=_instrument) as run:
            if not run.ok:
                return spans.unmeasured(self.name)
            return self._read(run)

    def _read(self, run) -> spans.Reach:
        """Run gcov over whatever notes files the build produced. -> the reach, or unmeasured."""
        notes = glob.glob(run.path("*.gcno"))
        if not notes:
            return spans.unmeasured(self.name)

        # THE DATA FILE, NOT JUST THE NOTES. `.gcno` is written by the COMPILER and says only that
        # the build was instrumented; `.gcda` is written by the PROGRAM as it exits and is the only
        # evidence it ran at all. A subject that segfaults on its first probe flushes no .gcda, and
        # gcov then reports every line of it as executed zero times -- a perfectly well-formed
        # answer that is indistinguishable from a corpus which reaches nothing.
        #
        # Reporting that as a measured zero is the exact failure this package is written against:
        # adequacy would read it as a broken tracer and refuse the task, when what actually happened
        # is that the subject crashed. Unmeasured is the honest verdict, and the crash is a finding
        # for the freeze stage, which is where a subject that will not run is properly diagnosed.
        if not glob.glob(run.path("*.gcda")):
            return spans.unmeasured(self.name)
        try:
            subprocess.run(["gcov", "-i", "-b"] + [os.path.basename(n) for n in notes],
                           cwd=run.work, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            return spans.unmeasured(self.name)

        wanted = run.subject_name()
        per_file: dict = {}
        for report in glob.glob(run.path("*.gcov.json.gz")):
            try:
                with gzip.open(report, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            for entry in payload.get("files", ()):
                name = os.path.basename(str(entry.get("file", "")))
                # Only the subject. The shim is our code and grading how much of IT ran would put a
                # denominator in the report that no corpus of the subject could ever move.
                if name != wanted:
                    continue
                executed, executable = set(), set()
                for line in entry.get("lines", ()):
                    number = int(line.get("line_number", 0))
                    if not number:
                        continue
                    executable.add(number)
                    if int(line.get("count", 0)) > 0:
                        executed.add(number)
                if executable:
                    previous = per_file.get(name, (set(), set()))
                    per_file[name] = (previous[0] | executed, previous[1] | executable)

        if not per_file:
            # gcov ran and said nothing about the subject: the notes and data were not found
            # together, which is the trap in the module docstring. UNMEASURED, never zero.
            return spans.unmeasured(self.name)
        return spans.assemble(self.name, per_file)


def _instrument(workdir: str, build: list, argv: list):
    """Add the profiling flags to whatever the shim table said to compile with.

    The build command is amended rather than replaced so that this stays correct when the shim table
    changes -- the C++ entry compiles in two steps, and a rewrite here would have to know that.
    """
    amended = []
    for command in build:
        amended.append(list(command) + list(COVERAGE_FLAGS))
    return amended, argv
