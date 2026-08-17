"""Line coverage for a Java subject, through a JVMTI agent this package builds itself.

Java is the one language here where the obvious tool is not an option. Its standard library has no
coverage facility, and JaCoCo -- what everyone uses -- is a third-party jar that would have to be
vendored into every offline task image. What a JDK does ship is JVMTI and the headers to write an
agent against it, so `agents/jvmti_lines.c` is that agent and this module compiles it on first use.

WHAT THAT BUYS, beyond avoiding a dependency: the denominator is the JVM's own line-number table
rather than a guess about which lines of the source are executable. That is the same quality of
answer gcov and Ruby give, and better than the heuristic the JavaScript backend has to fall back on.

TWO TOOLCHAINS, AND EITHER CAN BE ABSENT. This needs javac to build the subject and a C compiler to
build the agent, and a machine with only one of them is perfectly ordinary. Both absences return
UNMEASURED, which is an absence the report states -- never a zero, which would mean an instrumented
run found nothing executed and would fail adequacy for a missing compiler.

THE AGENT IS BUILT ONCE AND CACHED beside this file. Building it per measurement would add a
compile to every task, and the source never changes between them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from . import driver, spans

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_SOURCE = os.path.join(HERE, "agents", "jvmti_lines.c")
AGENT_LIBRARY = os.path.join(HERE, "agents", "libfrfcov.so")

REPORT = "coverage.json"

# Where a JDK keeps jvmti.h. Probed rather than assumed: the layout is stable but the prefix is not,
# and hard-coding one distribution's path is how this would work on the machine it was written on.
_HEADER = "jvmti.h"


class JavaCoverage:
    """Line coverage from a JVMTI agent, compiled on demand."""

    name = "jvmti-lines"
    language = "java"

    def measure(self, spec, probes) -> spans.Reach:
        agent = build_agent()
        if not agent:
            return spans.unmeasured(self.name)

        with driver.Run(spec, probes, language=self.language,
                        build_override=_attach(agent)) as run:
            if not run.ok:
                return spans.unmeasured(self.name)
            return self._read(run)

    def _read(self, run) -> spans.Reach:
        path = run.path(REPORT)
        if not os.path.exists(path):
            return spans.unmeasured(self.name)
        try:
            payload = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return spans.unmeasured(self.name)

        executable = {int(n) for n in payload.get("executable", ())}
        executed = {int(n) for n in payload.get("executed", ())}
        if not executable:
            # The agent attached but saw no line table for the class, which means the subject was
            # compiled without `-g` or never loaded. Unmeasured, not zero.
            return spans.unmeasured(self.name)
        return spans.assemble(self.name, {run.subject_name(): (executed, executable)})


def build_agent() -> str:
    """-> the path to the built agent, or "" when it cannot be built here.

    Cached: the source does not change between measurements, and compiling it per task would put a
    C build in front of every Java subject.
    """
    if os.path.exists(AGENT_LIBRARY):
        return AGENT_LIBRARY
    includes = _jdk_includes()
    compiler = shutil.which("cc") or shutil.which("gcc")
    if not includes or not compiler or not os.path.exists(AGENT_SOURCE):
        return ""

    command = [compiler, "-shared", "-fPIC", "-O2"]
    for directory in includes:
        command += ["-I", directory]
    command += ["-o", AGENT_LIBRARY, AGENT_SOURCE]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return ""
    return AGENT_LIBRARY if done.returncode == 0 and os.path.exists(AGENT_LIBRARY) else ""


def _jdk_includes() -> list:
    """The JDK's include directories, found from javac rather than from a guessed prefix."""
    javac = shutil.which("javac")
    if not javac:
        return []
    home = os.path.dirname(os.path.dirname(os.path.realpath(javac)))
    include = os.path.join(home, "include")
    if not os.path.exists(os.path.join(include, _HEADER)):
        return []
    # The platform subdirectory holds jni_md.h, which jni.h includes by name alone.
    platform = os.path.join(include, "linux")
    return [include, platform] if os.path.isdir(platform) else [include]


def _attach(agent: str):
    """Return a build_override that adds `-g` to javac and the agent to the java command.

    `-g` is not decoration: without debug information the class carries no line-number table, the
    agent has nothing to set breakpoints on, and the measurement comes back empty for a reason that
    looks exactly like a subject that never ran.
    """
    def override(workdir: str, build: list, argv: list):
        compiled = [list(command) + ["-g"] if command and command[0] == "javac" else list(command)
                    for command in build]
        # The subject's class name is the file's stem, which is what the agent is told to watch.
        klass = "Subject"
        instrumented = list(argv)
        if instrumented and instrumented[0] == "java":
            instrumented.insert(1, "-agentpath:%s=%s,%s"
                                % (agent, klass, os.path.join(workdir, REPORT)))
        return compiled, instrumented
    return override
