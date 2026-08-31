"""Widen GitHub repositories into package-surface candidates.

The repository search remains the enumerable source. This adapter only performs mechanical checkout
inspection: it finds a Python package tree and records public top-level callables as a dispatch table.
The package's probe generator is deliberately not invented here; Package asks the model to draft it
from this grounded surface, then runs that code only inside E2B.
"""
from __future__ import annotations

import ast
import os
import sys
import shutil
import subprocess
import warnings

from ..core.scale import Candidate
from .function_miner import canonical

CLONE_TIMEOUT = 300.0
PER_REPOSITORY = 4
MAX_REPOSITORY_KB = 100_000
MAX_SOURCE_FILE_BYTES = 2_000_000
_SKIP = {"tests", "test", "testing", "docs", "examples", "benchmarks", "bench", "conftest.py", "solutions", "spider", "ci", "dev_tools", "tools"}


# One search request buys many rows; inspecting a row is a clone. See `function_miner`.
SEARCH_PAGE = 50
INSPECT_AT_ONCE = 4


class GitHubPackages:
    name = "github-packages"

    # Widening, so its page numbers mean nothing to a later process -- see `GitHubFunctions`, which
    # carries the full account. The repository page walked to is what survives, and is the cursor.
    resumable_pages = False

    @property
    def cursor_key(self) -> str:
        """What this instance is actually walking. The inner index names its topics and language."""
        return "%s/%s" % (self.name, getattr(self._index, "name", ""))

    @property
    def cursor(self) -> int:
        return self._source_page

    @cursor.setter
    def cursor(self, value: int) -> None:
        self._source_page = max(int(value or 0), self._source_page)

    def __init__(self, index, *, workspace: str = "") -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "package-checkouts")
        self._expanded = []
        self._rows: list = []
        self._source_page = 0
        self._exhausted = False
        self.rejection_counts: dict[str, int] = {}

    def total(self):
        return self._index.total()

    def page(self, number: int, *, size: int = 20):
        needed = (number + 1) * size
        while len(self._expanded) < needed and not self._exhausted:
            # Big search pages, small inspection batches -- see `function_miner`, which carries the
            # account. A request that bought four rows was the batch's throughput once search was
            # serialised against GitHub's secondary rate limit.
            if not self._rows:
                self._rows = list(self._index.page(self._source_page, size=SEARCH_PAGE))
                self._source_page += 1
                if not self._rows:
                    self._exhausted = True
                    break
            rows, self._rows = self._rows[:INSPECT_AT_ONCE], self._rows[INSPECT_AT_ONCE:]
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
        language = canonical(repository.language)
        from .package_adapters import operations as _operations, supported as _adapters_supported
        if not url or not commit or not _adapters_supported(language):
            self.rejection_counts["unsupported-or-unpinned"] = self.rejection_counts.get("unsupported-or-unpinned", 0) + 1
            return None
        root = self._materialise(url, commit)
        if root is None:
            self.rejection_counts["checkout-failed"] = self.rejection_counts.get("checkout-failed", 0) + 1
            return None
        package_root, package_name = _find_package_root(root, language)
        if not package_root or not package_name:
            self.rejection_counts["no-package-root"] = self.rejection_counts.get("no-package-root", 0) + 1
            return None
        # THE DISPATCHER CONTRACT IS ONE THING, AND THIS WAS THE SECOND COPY OF IT. The old line was
        # `_public_dispatch(...) if python else _javascript_dispatch(...)`, so ruby -- which has a
        # registered adapter in package_adapters.py -- fell through to the JAVASCRIPT branch and came
        # out empty rather than errored. `operations()` is the same function `Package._locate` uses,
        # so the source cannot describe a surface the locate side then fails to read. (For python the
        # answer is identical to the old local helper; verified before rerouting.)
        dispatch = _operations(root, language, package_name, package_root)
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
            scale="package", language=("javascript" if language == "javascript" else language), source="github-packages",
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


def _find_package_root(root: str, language: str = "python"):
    if language in ("javascript", "typescript"):
        manifest = os.path.join(root, "package.json")
        if os.path.isfile(manifest):
            try:
                import json
                data = json.load(open(manifest, encoding="utf-8"))
                name = str(data.get("name") or "").split("/")[-1]
                if name:
                    return root, name
            except (OSError, ValueError, TypeError):
                return "", ""
        return "", ""
    # A RUBY GEM IS A GEMFILE + lib/<something>.rb. `_ruby` walks `package_root` for `.rb` files and
    # expects them under it; the source tree can be a whole repo, so `lib/` is the package root. The
    # name is the gemspec's declared name, or the directory name when no gemspec is present -- the
    # surrounding repo's name is a fine key when the repo IS the gem.
    if language == "ruby":
        import glob
        gemspecs = glob.glob(os.path.join(root, "*.gemspec"))
        name = ""
        if gemspecs:
            try:
                import re as _re
                spec_text = open(gemspecs[0], encoding="utf-8", errors="replace").read()
                match = _re.search(r"\.name\s*=\s*[\"']([^\"']+)[\"']", spec_text)
                name = match.group(1) if match else ""
            except OSError:
                name = ""
        if not name:
            name = os.path.basename(root.rstrip("/\\"))
        lib = os.path.join(root, "lib")
        if os.path.isdir(lib) and any(f.endswith(".rb") for f in os.listdir(lib)):
            return lib, name
        return "", ""
    # A STATIC LANGUAGE'S PACKAGE ROOT IS ITS MANIFEST'S DIRECTORY, not an __init__.py hunt.
    # Go has go.mod, Rust Cargo.toml, C++ CMakeLists.txt (or a src/ dir). The old fall-through made
    # every Go batch walk the WHOLE repository looking for __init__.py -- TheAlgorithms/Go is 140MB
    # and hundreds of files, and each widened candidate paid that walk, which is what produced the
    # twenty-minute stalls with zero CPU and no network: an os.walk with no subprocess to show for it.
    if language in ("go", "rust", "cpp", "c"):
        manifests = {"go": ("go.mod",), "rust": ("Cargo.toml",), "cpp": ("CMakeLists.txt",),
                     "c": ("CMakeLists.txt",)}
        for manifest in manifests.get(language, ()):
            found_manifest = os.path.join(root, manifest)
            if os.path.isfile(found_manifest):
                return root, os.path.basename(root)
        # No manifest at root: src/ is the conventional C/C++/Rust layout.
        for candidate in ("src",):
            if os.path.isdir(os.path.join(root, candidate)):
                return os.path.join(root, candidate), os.path.basename(root)
        return "", ""
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


def _javascript_dispatch(root: str, package_root: str, package_name: str):
    from .package_adapters import _javascript
    return _javascript(root, package_name, package_root)


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
            imported = set()
            for child in ast.walk(tree):
                if isinstance(child, ast.Import):
                    imported.update(alias.name.split(".", 1)[0] for alias in child.names)
                elif isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
                    imported.add(child.module.split(".", 1)[0])
            own = {package_name.lower().replace("-", "_")}
            foreign = {name for name in imported
                       if name not in own and name not in getattr(sys, "stdlib_module_names", ())}
            if foreign:
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
