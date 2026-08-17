"""Line coverage for a Rust subject, through rustc's LLVM source-based coverage.

`-C instrument-coverage`, then the LLVM tools that ship with the toolchain read the counters back.
No third-party crate, which matters more here than elsewhere: these tasks build offline, and the
usual answer (`cargo llvm-cov`, `tarpaulin`) is a crate that would have to be vendored.

THE TOOLS ARE OPTIONAL AND THIS BACKEND SAYS SO. `llvm-profdata` and `llvm-cov` come from the
`llvm-tools` rustup component, which is not installed by default. When they are missing the answer
is UNMEASURED -- an absence the report states -- and not a zero, which would mean an instrumented
run found nothing executed and would fail adequacy for a component nobody installed.

FINDING THEM IS THE FIDDLY PART. They are not on PATH: they live inside the active toolchain, under
`lib/rustlib/<target>/bin`. `rustc --print target-libdir` gives the directory next door, which is
the only stable way to locate them without assuming a toolchain name.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess

from . import driver, spans

PROFRAW = "subject.profraw"


class RustCoverage:
    """LLVM source-based coverage, as rustc emits it."""

    name = "rust-llvm-cov"
    language = "rust"

    def measure(self, spec, probes) -> spans.Reach:
        tools = _llvm_tools()
        if not tools:
            # The toolchain is here but its coverage component is not. Stated as an absence rather
            # than measured as a zero -- see the module docstring.
            return spans.unmeasured(self.name)

        with driver.Run(spec, probes, language=self.language,
                        extra_env={"LLVM_PROFILE_FILE": PROFRAW},
                        build_override=_instrument) as run:
            if not run.ok:
                return spans.unmeasured(self.name)
            return self._read(run, tools)

    def _read(self, run, tools: dict) -> spans.Reach:
        raw = glob.glob(run.path("*.profraw"))
        if not raw:
            return spans.unmeasured(self.name)
        merged = run.path("subject.profdata")
        binary = run.path("serve.bin")
        try:
            done = subprocess.run([tools["profdata"], "merge", "-sparse"] + raw + ["-o", merged],
                                  cwd=run.work, capture_output=True, text=True, timeout=300)
            if done.returncode != 0 or not os.path.exists(merged):
                return spans.unmeasured(self.name)
            done = subprocess.run([tools["cov"], "export", "-instr-profile=%s" % merged, binary,
                                   "--format=text"],
                                  cwd=run.work, capture_output=True, text=True, timeout=300)
            if done.returncode != 0:
                return spans.unmeasured(self.name)
            payload = json.loads(done.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return spans.unmeasured(self.name)

        wanted = run.subject_name()
        executed, executable = set(), set()
        for data in payload.get("data", ()):
            for entry in data.get("files", ()):
                if os.path.basename(str(entry.get("filename", ""))) != wanted:
                    continue
                # A segment is [line, column, count, hasCount, isRegionEntry, isGapRegion]. Only
                # the ones that carry a count say anything about whether a line ran; the others mark
                # where a region ends and would report every closing position as unreached.
                for segment in entry.get("segments", ()):
                    if len(segment) < 4 or not segment[3]:
                        continue
                    line = int(segment[0])
                    executable.add(line)
                    if int(segment[2]) > 0:
                        executed.add(line)

        if not executable:
            return spans.unmeasured(self.name)
        return spans.assemble(self.name, {wanted: (executed, executable)})


def _llvm_tools() -> dict:
    """Where this toolchain keeps llvm-profdata and llvm-cov, or {} when it does not.

    Located relative to what rustc reports rather than searched for by name. A copy found on PATH
    could belong to a different LLVM than the one rustc used, and a version mismatch there produces
    a profdata error rather than a wrong number -- but only after a build, which is a slow way to
    discover the component is missing.
    """
    try:
        done = subprocess.run(["rustc", "--print", "target-libdir"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0:
        return {}
    binaries = os.path.join(os.path.dirname(done.stdout.strip()), "bin")
    profdata = os.path.join(binaries, "llvm-profdata")
    cov = os.path.join(binaries, "llvm-cov")
    if os.path.exists(profdata) and os.path.exists(cov):
        return {"profdata": profdata, "cov": cov}
    return {}


def _instrument(workdir: str, build: list, argv: list):
    """Add the instrumentation flag to whatever the shim table said to compile with."""
    return [list(command) + ["-C", "instrument-coverage"] for command in build], argv
