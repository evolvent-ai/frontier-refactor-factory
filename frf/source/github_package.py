"""Widen GitHub repositories into package-surface candidates.

The repository search remains the enumerable source. This adapter only performs mechanical checkout
inspection: it finds a Python package tree and records public top-level callables as a dispatch table.
The package's probe generator is deliberately not invented here; Package asks the model to draft it
from this grounded surface, then runs that code only inside E2B.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import warnings

from ..core.scale import Candidate

CLONE_TIMEOUT = 300.0
PER_REPOSITORY = 4
MAX_REPOSITORY_KB = 100_000
MAX_SOURCE_FILE_BYTES = 2_000_000
_SKIP = {"tests", "test", "testing", "docs", "examples", "benchmarks", "bench", "conftest.py", "solutions", "spider", "ci", "dev_tools", "tools"}


class GitHubPackages:
    name = "github-packages"

    def __init__(self, index, *, workspace: str = "") -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "package-checkouts")
        self._expanded = []
        self._source_page = 0
        self._exhausted = False
        self.rejection_counts: dict[str, int] = {}

    def total(self):
        return self._index.total()

    def page(self, number: int, *, size: int = 20):
        needed = (number + 1) * size
        while len(self._expanded) < needed and not self._exhausted:
            rows = list(self._index.page(
                self._source_page, size=max(1, min(4, needed - len(self._expanded)))))
            self._source_page += 1
            if not rows:
                self._exhausted = True
                break
            for row in rows:
                candidate = self._inspect(row)
                if candidate is not None:
                    self._expanded.append(candidate)
        start = number * size
        return self._expanded[start:start + size]

    def _inspect(self, repository: Candidate):
        detail = repository.detail or {}
        size_kb = int(detail.get("size_kb") or 0)
        if size_kb > MAX_REPOSITORY_KB:
            self.rejection_counts["repository-too-large"] = self.rejection_counts.get("repository-too-large", 0) + 1
            return None
        url = str(detail.get("repository") or "")
        commit = str(detail.get("commit") or "")
        identity = str(detail.get("identity") or "")
        if not url or not commit or repository.language.lower() not in ("python", ""):
            self.rejection_counts["unsupported-or-unpinned"] = self.rejection_counts.get("unsupported-or-unpinned", 0) + 1
            return None
        root = self._materialise(url, commit)
        if root is None:
            self.rejection_counts["checkout-failed"] = self.rejection_counts.get("checkout-failed", 0) + 1
            return None
        package_root, package_name = _find_package_root(root)
        if not package_root or not package_name:
            self.rejection_counts["no-package-root"] = self.rejection_counts.get("no-package-root", 0) + 1
            return None
        dispatch = _public_dispatch(root, package_root, package_name)
        # The call seam is JSON-only. Do not ask the model to invent an encoding for bytes, paths,
        # handles, or other non-JSON arguments; retain only operations the adapter proved safe.
        dispatch = [entry for entry in dispatch if bool(entry.get("json_safe", True))]
        if len(dispatch) < 4:
            self.rejection_counts["surface-too-small"] = self.rejection_counts.get("surface-too-small", 0) + 1
            return None
        # A package task must expose a real surface, not test helpers or a single tiny utility.
        # Keep a bounded but broad contract; the generator sees all retained operations.
        dispatch = dispatch[:40]
        return Candidate(
            identity="github:%s@%s" % (identity, commit[:12]),
            scale="package", language="python", source="github-packages",
            detail={
                "root": root,
                "package_name": package_name,
                "package_root": package_root,
                "entry_points": [x["name"] for x in dispatch],
                "dispatch": dispatch,
                "generator": "",
                "description": str(detail.get("description") or identity),
                "forbidden": [package_name],
                "commit": commit,
                "repository": url,
            })

    def _materialise(self, url: str, commit: str):
        room = os.path.join(self._workspace, commit[:16])
        if os.path.isdir(os.path.join(room, ".git")):
            return room
        os.makedirs(room, exist_ok=True)
        try:
            for argv in (["git", "init", "--quiet"],
                         ["git", "remote", "add", "origin", url],
                         ["git", "fetch", "--quiet", "--depth", "1", "origin", commit],
                         ["git", "checkout", "--quiet", "FETCH_HEAD"]):
                done = subprocess.run(argv, cwd=room, capture_output=True, text=True,
                                      timeout=CLONE_TIMEOUT)
                if done.returncode != 0:
                    shutil.rmtree(room, ignore_errors=True)
                    return None
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(room, ignore_errors=True)
            return None
        return room


def _find_package_root(root: str):
    candidates = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "tests", "test", "docs", "examples")]
        if "__init__.py" not in files:
            continue
        rel = os.path.relpath(directory, root)
        if rel == "." or rel.startswith("."):
            continue
        parts = rel.split(os.sep)
        if any(part in _SKIP for part in parts) or parts[-1].startswith("_"):
            continue
        candidates.append((len(parts), directory, parts[-1]))
    if not candidates:
        return "", ""
    # The package boundary is the shallowest non-private package directory. Choosing the deepest
    # one accidentally selected scipy._lib.common instead of scipy, turning an entire public library
    # into an internal helper task.
    _, directory, name = sorted(candidates, key=lambda x: (x[0], x[1]))[0]
    return directory, name


def _public_dispatch(root: str, package_root: str, package_name: str):
    result = []
    seen = set()
    checked = 0
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not d.startswith("_") and d not in _SKIP]
        for filename in sorted(files):
            if checked >= 500 or len(result) >= 40:
                return result
            if not filename.endswith(".py") or filename.startswith("_") or filename in _SKIP:
                continue
            # Test modules and test-named public functions are not package contract operations.
            if filename.startswith("test_") or filename.endswith("_test.py"):
                continue
            checked += 1
            path = os.path.join(directory, filename)
            try:
                if os.path.getsize(path) > MAX_SOURCE_FILE_BYTES:
                    continue
                # Legacy escapes are irrelevant to AST discovery but otherwise flood batch logs.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(open(path, encoding="utf-8", errors="replace").read(), path)
            except (OSError, SyntaxError, ValueError):
                continue
            rel = os.path.relpath(path, package_root)[:-3].replace(os.sep, ".")
            module_name = package_name + ("." + rel if rel != "__init__" else "")
            for node in tree.body:
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not node.name.startswith("_") and node.name not in seen):
                    seen.add(node.name)
                    # JSON seam cannot represent a set/bytes result. Mark obvious set literals as
                    # unsafe so the package source filter rejects them before LLM/E2B work.
                    # Reject obvious non-wire values before paying for a model generator. Sets,
                    # pathlib/file objects, and user-defined class instances cannot cross the JSON
                    # call seam; allowing them merely defers a deterministic material failure to
                    # freeze.
                    unsafe = any(isinstance(child, ast.Set) for child in ast.walk(node))
                    unsafe = unsafe or any(
                        isinstance(child, ast.Call) and (
                            (isinstance(child.func, ast.Name) and
                             (child.func.id[:1].isupper() or child.func.id in {"open", "Path"}))
                            or (isinstance(child.func, ast.Attribute) and
                                child.func.attr in {"Path", "open"})
                        ) for child in ast.walk(node))
                    result.append({"name": node.name, "module": module_name,
                                   "symbol": node.name, "signature": _signature(node),
                                   "json_safe": not unsafe})
                    if len(result) >= 40:
                        return result
    return result


def _signature(node):
    args = [a.arg for a in list(node.args.posonlyargs) + list(node.args.args)]
    return "(" + ", ".join(args) + ")"
