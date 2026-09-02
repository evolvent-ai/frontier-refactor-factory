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
# WHERE A PROJECT WRITES DOWN HOW TO RUN ITSELF. Scripts were scanned and prose was not, which
# leaves out the one file every project has and writes for humans: the README. Its fenced code
# blocks are worked examples -- the maintainer's own invocations, with real arguments, known to run.
#
# It is the answer to the largest remaining refusal. Thirteen candidates in one batch lifted
# scenarios, tried every one, and had none that did anything: `ran but did nothing`. A tool that
# needs a subcommand cannot be invoked by guessing, and the README is where the subcommand is
# written.
_SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".fish", ".py", ".ps1")
_PROSE_SUFFIXES = (".md", ".markdown", ".rst")

# CI IS WHERE A PROJECT RUNS ITSELF FOR REAL. A workflow's `run:` steps are invocations the
# maintainer relies on passing, with the arguments they actually use -- the same evidence a README
# gives, written by the same people, and kept correct because it breaks the build when it is not.
#
# Thirty-one candidates in one batch were refused at `corpus-too-thin` within striking distance of
# the threshold, thirteen of them at exactly nine scenarios. Supply is what that needs, and this is
# the last untapped source of it.
_CI_SUFFIXES = (".yml", ".yaml")

# TWO SOURCES EVERY PROJECT HAS, and neither has a suffix the scanner was looking for. A
# `package.json` script body and a Makefile recipe line ARE invocations -- `"lint": "eslint ."`,
# `\ttool build $(SRC)` -- written by the maintainer and kept working because the project runs them.
#
# 46% of the candidates that got past the build were refused for having no invocation that ran. The
# harvest was reading scripts, READMEs and CI, and walking past the two files in the root.
_SCRIPT_FILES = ("Makefile", "makefile", "GNUmakefile", "package.json")
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



def _fenced(lines: list) -> list:
    """The lines inside ``` fences, with everything else blanked out.

    Blanked rather than removed so that a line number still points at the line it came from: a
    harvested command records where it was found, and an off-by-many is worse than no number.
    """
    kept, inside = [], False
    for line in lines:
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            inside = not inside
            kept.append("")
            continue
        kept.append(line if inside else "")
    return kept


def _unprompted(raw: str) -> str:
    """`$ tool --flag` -> `tool --flag`. A shell prompt is not part of the command."""
    stripped = raw.strip()
    for prompt in ("$ ", "> ", "% ", "# "):
        if stripped.startswith(prompt):
            return stripped[len(prompt):]
    return raw



def _run_steps(lines: list) -> list:
    """The shell lines of a CI workflow's `run:` steps, with everything else blanked out.

    Handles both `run: tool --flag` and the block form, where `run: |` is followed by an indented
    script. Blanked rather than removed so a lifted command still records the line it came from.
    """
    kept, block_indent = [], None
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if stripped and indent <= block_indent:
                block_indent = None
            else:
                kept.append(line)
                continue
        if stripped.startswith("run:") or stripped.startswith("- run:"):
            body = stripped.split("run:", 1)[1].strip()
            if body in ("|", ">", "|-", ">-", "|+"):
                block_indent = indent
                kept.append("")
            else:
                kept.append(body)
            continue
        kept.append("")
    return kept



# What a documentation placeholder looks like. A README shows the SHAPE of a command --
# `tool --log_file <path_to_log_file>` -- and that is not a command: the file does not exist, the
# program exits 2, and the scale spends a smoke run finding out. Four of thirteen invocations lifted
# in one batch were templates like this, and they crowded out the ones that would have run.
_PLACEHOLDER_MARKS = ("<", ">", "[", "]", "{", "}", "...", "path_to_", "your_", "your-",
                      "PATH_TO", "YOUR_", "example.com", "/path/")


def _is_runnable(argv: list) -> bool:
    """Whether a lifted line is a command rather than a description of one.

    Also rejects assignments: a fenced block in a README is often a dependency list, and
    `thing = 0.1.0` splits into a perfectly good argv that names the program and runs nothing.
    """
    if len(argv) > 1 and argv[1] == "=":
        return False
    for token in argv[1:]:
        if any(mark in token for mark in _PLACEHOLDER_MARKS):
            return False
        if token.isupper() and len(token) > 2 and token.isalpha():
            return False           # `tool FILE`, the other way a manual writes a placeholder
    return True



def _json_script_bodies(lines: list) -> list:
    """The command lines inside a package.json `scripts` block, everything else blanked.

    Blanked rather than dropped so a lifted command still records the line it came from. Parsed as
    JSON when it can be -- a manifest that does not parse is not worth guessing at.
    """
    import json as _json

    try:
        data = _json.loads("\n".join(lines))
    except Exception:                                      # noqa: BLE001 -- not our manifest to fix
        return [""] * len(lines)
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return [""] * len(lines)
    # RETURNED AS THEIR OWN LINES, not matched back into the file's. A package.json is often
    # written on one physical line, so a line-by-line match finds the first script body and loses
    # the rest. The line number a lifted command records is therefore approximate for a manifest,
    # which is the right trade: the number is provenance, the command is the material.
    #
    # `npm run x && npm run y` is a compound command; the harvest's own shell-operator rejection
    # drops those, so only the plain ones survive.
    bodies = [str(v) for v in scripts.values() if isinstance(v, str)]
    return bodies + [""] * max(0, len(lines) - len(bodies))


def _make_recipes(lines: list) -> list:
    """A Makefile's recipe lines -- the tab-indented ones -- with targets and variables blanked."""
    kept = []
    for line in lines:
        if line.startswith("\t"):
            body = line[1:].strip()
            # `@` silences a recipe line and `-` ignores its failure; neither is part of the command.
            kept.append(body.lstrip("@-").strip())
        else:
            kept.append("")
    return kept


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
            prose = filename.endswith(_PROSE_SUFFIXES)
            workflow = filename.endswith(_CI_SUFFIXES)
            manifest = filename in _SCRIPT_FILES
            if not (prose or workflow or manifest or filename.endswith(_SCRIPT_SUFFIXES)):
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
            if prose:
                # ONLY WHAT IS INSIDE A FENCE. Prose is full of sentences that shlex will happily
                # split into an argv, and a sentence mentioning the program's name is not an
                # invocation of it.
                lines = _fenced(lines)
            elif manifest:
                lines = (_json_script_bodies(lines) if filename == "package.json"
                         else _make_recipes(lines))
            elif workflow:
                # ONLY WHAT FOLLOWS `run:`. A workflow is mostly configuration, and a `name:` or an
                # `if:` that happens to mention the program is not a way to invoke it.
                lines = _run_steps(lines)
            for lineno, raw in enumerate(lines, 1):
                raw = _unprompted(raw)
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
                if not _is_runnable(argv):
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
