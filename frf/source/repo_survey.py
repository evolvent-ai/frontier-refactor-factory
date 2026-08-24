"""Mechanical survey of a pinned repository before the repo scale spends E2B time.

A GitHub search result says only that a repository exists. RepoSurvey records the facts needed to
choose a real program/workload: build markers, executable layouts, project-owned corpus and obvious
non-program signals. It never decides what the program means and never executes repository code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoSurvey:
    root: str
    languages: tuple[str, ...] = ()
    build_markers: tuple[str, ...] = ()
    binaries: tuple[str, ...] = ()
    test_dirs: tuple[str, ...] = ()
    corpus_dirs: tuple[str, ...] = ()
    benchmark_dirs: tuple[str, ...] = ()
    input_files: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    size_bytes: int = 0
    source_files: int = 0

    @property
    def has_executable_shape(self) -> bool:
        return bool(self.binaries or any(x in self.build_markers for x in (
            "Dockerfile", "pyproject.toml", "package.json", "Makefile")))

    @property
    def has_workload(self) -> bool:
        return bool(self.input_files or self.benchmark_dirs or self.corpus_dirs or self.test_dirs)

    def to_json(self) -> dict:
        return {"root": self.root, "languages": list(self.languages),
                "build_markers": list(self.build_markers), "binaries": list(self.binaries),
                "test_dirs": list(self.test_dirs), "corpus_dirs": list(self.corpus_dirs),
                "benchmark_dirs": list(self.benchmark_dirs), "input_files": list(self.input_files),
                "signals": list(self.signals), "size_bytes": self.size_bytes,
                "source_files": self.source_files,
                "has_executable_shape": self.has_executable_shape,
                "has_workload": self.has_workload}


def survey(root: str, *, max_files: int = 100_000, max_input_bytes: int = 262_144) -> RepoSurvey:
    markers = ("Cargo.toml", "go.mod", "go.work", "CMakeLists.txt", "Makefile", "configure",
               "pyproject.toml", "setup.py", "package.json", "Dockerfile")
    found_markers = tuple(m for m in markers if os.path.exists(os.path.join(root, m)))
    langs = set()
    if "Cargo.toml" in found_markers: langs.add("rust")
    if "go.mod" in found_markers or "go.work" in found_markers: langs.add("go")
    if "pyproject.toml" in found_markers or "setup.py" in found_markers: langs.add("python")
    if "package.json" in found_markers: langs.add("javascript")
    if "CMakeLists.txt" in found_markers or "configure" in found_markers: langs.add("cpp")

    binaries, tests, corpus, benchmarks, inputs, signals = [], [], [], [], [], []
    total, source_count = 0, 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target", "build", ".venv", "venv")]
        rel_dir = os.path.relpath(directory, root)
        parts = set(rel_dir.split(os.sep))
        low = rel_dir.lower()
        if parts & {"tests", "test", "spec", "t"}: tests.append(rel_dir)
        if parts & {"testdata", "fixtures", "fixture", "corpus", "fate", "regression"}: corpus.append(rel_dir)
        if parts & {"bench", "benchmarks", "benchmark", "perf", "performance"}: benchmarks.append(rel_dir)
        if any(x in low for x in ("tui", "terminal-ui", "electron", "tauri")): signals.append("interactive-or-gui")
        for name in files:
            total += 1
            if total > max_files: break
            full = os.path.join(directory, name)
            try: size = os.path.getsize(full)
            except OSError: continue
            if name.endswith((".py", ".go", ".rs", ".c", ".cc", ".cpp", ".js", ".ts", ".java")): source_count += 1
            rel = os.path.relpath(full, root)
            if name == "main.go" and ("cmd" in parts or rel == "main.go"): binaries.append(rel)
            elif name == "main.rs": binaries.append(rel)
            elif name in ("Dockerfile",): binaries.append(rel)
            elif name.startswith("test") and name.endswith((".sh", ".py", ".go", ".rs")): tests.append(rel)
            if size and size <= max_input_bytes and name.endswith((".json", ".yaml", ".yml", ".toml", ".xml", ".txt", ".csv", ".md")):
                if not (parts & {"docs", "documentation", "vendor"}): inputs.append(rel)
        if total > max_files: break
    # Stable and bounded provenance; do not let a huge source tree become a giant candidate payload.
    return RepoSurvey(root, tuple(sorted(langs)), found_markers, tuple(sorted(set(binaries))[:100]),
                      tuple(sorted(set(tests))[:100]), tuple(sorted(set(corpus))[:100]),
                      tuple(sorted(set(benchmarks))[:100]), tuple(sorted(set(inputs))[:100]),
                      tuple(sorted(set(signals))), total_bytes(root), source_count)


def total_bytes(root: str) -> int:
    total = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target", "build")]
        for name in files:
            try: total += os.path.getsize(os.path.join(directory, name))
            except OSError: pass
    return total


def read_package_json(root: str) -> dict:
    path = os.path.join(root, "package.json")
    try:
        with open(path, encoding="utf-8") as handle: return json.load(handle)
    except (OSError, ValueError): return {}
