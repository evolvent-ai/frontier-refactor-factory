"""Repository-owned workload harvesting for the process seam.

This module never invents program semantics. It reads test scripts and corpus files that the pinned
repository already owns, extracts commands that mention the real program, and packages input files
as deterministic fixtures. Setup-only shell commands are not scored.
"""
from __future__ import annotations

import fnmatch
import io
import os
import re
import shlex
import tarfile
from dataclasses import dataclass, field


_NEVER = {".git", "node_modules", "target", "build", "dist", ".venv", "venv", "vendor"}
_SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".fish", ".py", ".ps1")
_INPUT_SUFFIXES = (".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".txt", ".csv", ".c", ".h", ".go", ".rs", ".py", ".js", ".ts")


@dataclass
class HarvestStats:
    files_scanned: int = 0
    commands_seen: int = 0
    commands_lifted: int = 0
    skipped_shell: int = 0
    scenarios_built: int = 0
    inputs_found: int = 0


@dataclass(frozen=True)
class Harvested:
    source: str
    argv: tuple[str, ...]
    line: int


def harvest_files(root: str, program_names: tuple[str, ...], *, max_files: int = 100,
                  max_commands: int = 200, stats: HarvestStats | None = None) -> list[Harvested]:
    """Extract invocations of a known program from repository-owned test scripts.

    Commands are parsed with shlex and retained only when one argv token is the known executable or
    its basename. This avoids treating setup commands (`mkdir`, `cp`, `export`) as behavior points.
    """
    names = {str(x) for x in program_names if x}
    names |= {os.path.basename(x) for x in names}
    found: list[Harvested] = []
    scanned = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NEVER]
        for filename in sorted(files):
            if not filename.endswith(_SCRIPT_SUFFIXES):
                continue
            if scanned >= max_files:
                return found
            scanned += 1
            path = os.path.join(directory, filename)
            try:
                lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            if stats:
                stats.files_scanned += 1
            for lineno, raw in enumerate(lines, 1):
                try:
                    argv = shlex.split(raw.strip())
                except ValueError:
                    continue
                if not argv:
                    continue
                if stats:
                    stats.commands_seen += 1
                token_names = {os.path.basename(token) for token in argv[:3]}
                if not (token_names & names):
                    continue
                if any(token in ("|", ">", ">>", "&&", ";") for token in argv):
                    if stats:
                        stats.skipped_shell += 1
                    continue
                found.append(Harvested(os.path.relpath(path, root), tuple(argv), lineno))
                if stats:
                    stats.commands_lifted += 1
                if len(found) >= max_commands:
                    return found
    return found


def harvest_corpus(root: str, *, directories: tuple[str, ...] = ("testdata", "fixtures", "corpus", "regression"),
                   max_files: int = 40, max_bytes: int = 262144,
                   stats: HarvestStats | None = None) -> list[str]:
    """Find repository-owned input files suitable for fixture scenarios."""
    found = []
    wanted = set(directories)
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NEVER]
        rel_parts = set(os.path.relpath(directory, root).split(os.sep))
        if not (rel_parts & wanted):
            continue
        for filename in sorted(files):
            if not filename.endswith(_INPUT_SUFFIXES):
                continue
            path = os.path.join(directory, filename)
            try:
                if 0 < os.path.getsize(path) <= max_bytes:
                    found.append(os.path.relpath(path, root))
            except OSError:
                continue
            if len(found) >= max_files:
                if stats:
                    stats.inputs_found = len(found)
                return found
    if stats:
        stats.inputs_found = len(found)
    return found


def fixture_archive(root: str, relatives: list[str], destination: str,
                    name: str = "inputs.tar.gz") -> str:
    """Write a deterministic, safe fixture archive and return its filename."""
    os.makedirs(destination, exist_ok=True)
    path = os.path.join(destination, name)
    root_abs = os.path.abspath(root)
    with tarfile.open(path, "w:gz") as archive:
        for relative in sorted(set(relatives)):
            full = os.path.abspath(os.path.join(root_abs, relative))
            if not full.startswith(root_abs + os.sep) or not os.path.isfile(full):
                continue
            data = open(full, "rb").read()
            info = tarfile.TarInfo(relative.replace(os.sep, "/"))
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return name


def scenarios_from_harvest(root: str, invocations: list[Harvested], *, fixture: str | None = None):
    """Convert harvested commands into the framework's Scenario objects."""
    from ..observe.process.runner import Scenario, Step
    scenarios = []
    for index, item in enumerate(invocations):
        argv = list(item.argv)
        # Replace the concrete executable with the seam token while retaining every real argument.
        try:
            pos = next(i for i, token in enumerate(argv) if os.path.basename(token) == os.path.basename(argv[0]))
        except StopIteration:
            pos = 0
        argv[pos] = "{PROGRAM}"
        scenarios.append(Scenario(probe_id="harvest-%04d" % index,
                                  steps=[Step(argv=argv)], fixture=fixture))
    return tuple(scenarios)


def grouped_input_scenarios(root: str, relatives: list[str], *, group_size: int = 1,
                            invocation: list[str] | None = None):
    """Create one scenario per deterministic group of corpus files."""
    from ..observe.process.runner import Scenario, Step
    invocation = list(invocation or ["{PROGRAM}"])
    result = []
    for start in range(0, len(relatives), group_size):
        group = relatives[start:start + group_size]
        result.append(Scenario(probe_id="corpus-%04d" % (start // group_size),
                               steps=[Step(argv=invocation + [relative]) for relative in group],
                               fixture="inputs-%04d.tar.gz" % (start // group_size)))
    return tuple(result)
