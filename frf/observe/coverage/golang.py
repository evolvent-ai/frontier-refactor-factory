"""Line coverage for a Go subject, through `go build -cover`.

NOT `go test -coverprofile`, which is the answer everyone reaches for first and is the wrong shape
here: it measures a TEST BINARY, and a subject served over the wire has no tests -- writing some
would mean measuring how much of the subject our own generated test exercised rather than how much
the corpus did. `go build -cover` (Go 1.20 and later) instruments an ordinary program, which is
exactly what the shim produces, and `GOCOVERDIR` collects the counters when it exits.

WHAT GO REPORTS. Statement blocks, as `file:startLine.col,endLine.col count hits`. A block with
`hits` zero never ran. That is a real denominator -- every block appears whether it ran or not --
so nothing here has to guess which lines are code.

A GO PROGRAM NEEDS A MODULE. `go build` in a directory with no `go.mod` fails with a message about
module mode that has nothing to do with coverage, so one is written alongside. It is the smallest
possible module and it is thrown away with the workspace.
"""
from __future__ import annotations

import os
import subprocess

from . import driver, spans

# The minimum module file a build needs. The name is arbitrary and never published.
GO_MOD = "module frfsubject\n\ngo 1.20\n"

COVERAGE_DIR = "gocoverdir"


class GoCoverage:
    """Go's own coverage instrumentation for ordinary binaries."""

    name = "go-cover"
    language = "go"

    def measure(self, spec, probes) -> spans.Reach:
        with driver.Run(spec, probes, language=self.language,
                        extra_env={"GOCOVERDIR": COVERAGE_DIR, "GOFLAGS": "-mod=mod"},
                        build_override=_instrument) as run:
            if not run.ok:
                return spans.unmeasured(self.name)
            return self._read(run)

    def _read(self, run) -> spans.Reach:
        profile = run.path("coverage.txt")
        try:
            subprocess.run(["go", "tool", "covdata", "textfmt",
                            "-i=%s" % run.path(COVERAGE_DIR), "-o=%s" % profile],
                           cwd=run.work, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            return spans.unmeasured(self.name)
        if not os.path.exists(profile):
            return spans.unmeasured(self.name)

        wanted = run.subject_name()
        executed, executable = set(), set()
        try:
            body = open(profile, encoding="utf-8").read()
        except OSError:
            return spans.unmeasured(self.name)

        for line in body.splitlines():
            if line.startswith("mode:") or ":" not in line:
                continue
            location, _, counts = line.rpartition(" ")
            location, _, _statements = location.rpartition(" ")
            path, _, span = location.partition(":")
            if os.path.basename(path) != wanted:
                continue
            first, last = _block(span)
            if first is None:
                continue
            executable.update(range(first, last + 1))
            if counts.strip() not in ("", "0"):
                executed.update(range(first, last + 1))

        if not executable:
            return spans.unmeasured(self.name)
        return spans.assemble(self.name, {wanted: (executed, executable)})


def _block(span: str):
    """`5.19,6.11` -> (5, 6). -> (None, None) when the shape is not what Go documents."""
    try:
        start, _, end = span.partition(",")
        return int(start.split(".")[0]), int(end.split(".")[0])
    except (ValueError, IndexError):
        return None, None


def _instrument(workdir: str, build: list, argv: list):
    """Replace the plain build with an instrumented one, and give the directory a module.

    A replacement rather than an amendment, unlike the gcc backend: `go build -cover` takes the
    package directory rather than a list of files, and appending a flag to the shim table's command
    would leave it naming files that module mode does not accept that way.
    """
    with open(os.path.join(workdir, "go.mod"), "w", encoding="utf-8") as handle:
        handle.write(GO_MOD)
    os.makedirs(os.path.join(workdir, COVERAGE_DIR), exist_ok=True)
    binary = os.path.join(workdir, "serve.bin")
    return [["go", "build", "-cover", "-o", binary, "."]], [binary]
