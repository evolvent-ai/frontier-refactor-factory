"""Solver-visible facts collected after the task writer has produced its workspace."""
from __future__ import annotations

import json
import os
import shlex
from collections import Counter
from dataclasses import replace
from pathlib import Path


_SKIP = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", "target"}
_SOURCE_SUFFIXES = {'.py', '.pyi', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.go', '.rs',
                    '.c', '.h', '.cc', '.cpp', '.hpp', '.java', '.rb', '.jl', '.zig', '.swift'}
_PROJECT_FILES = {'run.sh', 'task-interface.json', 'go.mod', 'go.sum', 'go.work', 'Cargo.toml',
                  'Cargo.lock', 'package.json', 'pyproject.toml', 'Makefile', 'CMakeLists.txt',
                  'README.md', 'README.rst', 'INSTALL.md'}


def workspace_paths(root):
    """A bounded selection of project entry files and source files across directories."""
    root = Path(root)
    declaration = root / 'task-interface.json'
    context = json.loads(declaration.read_text()) if declaration.is_file() else {}
    entries = []
    for command in (context.get('preparation_commands') or []) + (context.get('build_commands') or []):
        tokens = shlex.split(command) if isinstance(command, str) else command
        for token in tokens:
            token = str(token).replace('{ROOT}/', '').removeprefix('/app/')
            candidate = Path(token)
            if (candidate.parts and not candidate.is_absolute() and '..' not in candidate.parts
                    and not token.startswith('-') and (root / candidate).exists()):
                entries.append(candidate)
    candidates = []
    visited = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted((d for d in dirs if d not in _SKIP and not d.startswith('.')),
                         key=lambda d: (d not in {'src', 'cmd', 'lib', 'include'}, d))
        relative = Path(directory).relative_to(root)
        if len(relative.parts) >= 6:
            dirs[:] = []
        for name in sorted(files):
            visited += 1
            path = Path(directory) / name
            if not path.is_symlink() and not name.startswith('.') and name != 'Dockerfile':
                item = relative / name
                priority = (0 if not relative.parts and name in _PROJECT_FILES else
                            1 if path.suffix in _SOURCE_SUFFIXES and
                            any(item == entry or entry in item.parents for entry in entries) else
                            2 if path.suffix in _SOURCE_SUFFIXES else 3)
                candidates.append((priority, item))
            if visited >= 10000:
                break
        if visited >= 10000:
            break
    paths, selected_per_directory = [], Counter()
    while candidates and len(paths) < 80:
        priority, item = min(candidates, key=lambda value: (
            value[0], selected_per_directory[value[1].parent], len(value[1].parts), str(value[1])))
        candidates.remove((priority, item))
        selected_per_directory[item.parent] += 1
        paths.append('/app/' + item.as_posix())
    return paths


def collect(task_dir: str, spec):
    """Return a Spec with bounded, real workspace facts; never inspect the answer key."""
    root = Path(task_dir) / "environment"
    declaration = root / "task-interface.json"
    context = json.loads(declaration.read_text()) if declaration.is_file() else {}
    context["workspace_paths"] = workspace_paths(root) if root.is_dir() else []
    context["deliverables"] = ["/app/run.sh", "implementation and build inputs under /app"]
    context.setdefault("self_check_commands", [])
    environment = dict(spec.environment)
    environment["instruction_context"] = context
    return replace(spec, environment=environment)


def finish(task_dir: str, spec, facts) -> None:
    """Write the instruction against the actual delivered workspace, exactly once."""
    from .statement import generate_instruction
    enriched = collect(task_dir, spec)
    (Path(task_dir) / "instruction.md").write_text(
        generate_instruction(enriched, facts), encoding="utf-8")
