"""Line coverage for a JavaScript or TypeScript subject, using V8's own counters.

`NODE_V8_COVERAGE` rather than a third-party instrumenter, for the reason that runs through this
whole package: the factory has no runtime dependencies, and a coverage number is not worth acquiring
one. Setting that variable makes Node write V8's raw coverage to a directory on exit, which is the
same data every JavaScript coverage tool is built on.

WHAT V8 REPORTS, and why it needs converting. Not lines: byte RANGES into the file, each with an
execution count, nested so that an inner range overrides the one containing it. A function body is
one range with count 1 and the `else` inside it is a narrower range with count 0. So the conversion
is: take the file's own executable lines, then subtract the lines covered by ranges whose count is
zero. Doing it the other way round -- collecting lines from ranges with a positive count -- reports
100% for every file, because the outermost range always covers the whole of it and always ran.
"""
from __future__ import annotations

import glob
import json
import os

from . import driver, spans

# Where Node is told to leave the coverage files, relative to the workspace.
COVERAGE_DIR = "v8-coverage"


class NodeCoverage:
    """V8's coverage counters, read out of the directory Node writes them to."""

    name = "node-v8"

    def __init__(self, language: str = "javascript") -> None:
        # The same backend serves TypeScript: what is measured is the file the subject was served
        # from, and by the time Node loads it the offsets refer to whatever it actually ran.
        self.language = language

    def measure(self, spec, probes) -> spans.Reach:
        with driver.Run(spec, probes, language=self.language,
                        extra_env={"NODE_V8_COVERAGE": COVERAGE_DIR}) as run:
            if not run.ok:
                return spans.unmeasured(self.name)

            wanted = run.subject_name()
            starts = spans.line_index(run.source)
            executable = spans.executable_lines(run.source)
            unreached: set = set()
            found = False

            for path in glob.glob(os.path.join(run.path(COVERAGE_DIR), "*.json")):
                try:
                    payload = json.load(open(path, encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for script in payload.get("result", ()):
                    if not str(script.get("url", "")).endswith(wanted):
                        continue
                    found = True
                    for function in script.get("functions", ()):
                        for span in function.get("ranges", ()):
                            if span.get("count", 0) != 0:
                                continue
                            first = spans.line_of(starts, int(span.get("startOffset", 0)))
                            last = spans.line_of(starts,
                                                 max(0, int(span.get("endOffset", 0)) - 1))
                            unreached.update(range(first, last + 1))

            if not found:
                # The subject ran but V8 reported nothing about it. That is a failure to measure,
                # not a subject that executed nothing, and reporting it as zero would fail adequacy
                # for an instrumentation problem of ours.
                return spans.unmeasured(self.name)
            return spans.assemble(self.name, {wanted: (executable - unreached, executable)})
