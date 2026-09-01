"""Does a repository declare a runnable entry point? Asked before anything is cloned.

WHY THIS EXISTS. The repo scale needs programs, and a topic search returns libraries: 55% of one
batch's candidates were refused at `no discoverable entry point` -- sqlglot, gval, libopenapi,
mwparserfromhell -- every one of them correctly, and every one after a clone.

WHY IT DOES NOT REIMPLEMENT THE RULES. `repo._discover_entrypoint` knows twelve shapes an entry
point takes, and it has grown by measurement: `cmd/<name>/main.go` because matching `main.go` by
filename refused goawk; a gemspec's `exe/` because Ruby could otherwise produce no task at all.
A second copy of that judgement would drift from the first, and the drift would be invisible --
sourcing would reject material the scale would have accepted, and nothing would say so.

So the RULES stay where they are and only their INPUT changes. This fetches the handful of files
those rules read into a scratch tree and asks the same function. One rule set, two drivers -- the
pattern `functions.scan` already uses to serve two widening sources.

Measured on the refusals it exists to prevent: of 35 repositories the scale later refused for having
no entry point, this rejected 35. Its cost is a few requests against the core API, which allows
5000 an hour, rather than a clone and a tree walk.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile

# The files the twelve rules read. Fetched by content, because a rule that looks for
# `[project.scripts]` or an explicit `mainClass` cannot be answered by a filename.
_MANIFESTS = (
    "Dockerfile", "pyproject.toml", "setup.py", "Cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "CMakeLists.txt", "Makefile", "Justfile", "justfile", "Taskfile.yml",
    "package.json", "main.py", "__main__.py",
)

# Directories whose LISTING is the declaration: a `cmd/<name>/main.go`, a gemspec's `exe/`, a
# Rust `src/bin/`. Only the names are needed, so these cost one request each and no content.
_LISTINGS = ("cmd", "src", "exe", "bin", "docker", ".github/workflows")

# Nested files worth one request when their directory says they might be there.
_NESTED = ("src/main.rs", "src/main.py", "docker/Dockerfile")

# What a repository may cost to interrogate. A monorepo with sixty `cmd/` entries is not worth
# sixty requests to answer a yes/no question that its first entry already answers.
MAX_REQUESTS = 14


class _Tree:
    """One repository's root, fetched lazily and remembered."""

    def __init__(self, http, full_name: str, ref: str = "") -> None:
        self._http = http
        self._full = full_name
        self._ref = ("?ref=%s" % ref) if ref else ""
        self._spent = 0
        self.reachable = True

    def _get(self, path: str):
        if self._spent >= MAX_REQUESTS:
            return None
        self._spent += 1
        try:
            return self._http.json("https://api.github.com/repos/%s/contents/%s%s"
                                   % (self._full, path, self._ref))
        except Exception:                                  # noqa: BLE001 -- absent or unreachable
            return None

    def names(self, path: str = "") -> list:
        payload = self._get(path)
        if not isinstance(payload, list):
            return []
        return [str(entry.get("name", "")) for entry in payload if isinstance(entry, dict)]

    def text(self, path: str) -> str | None:
        payload = self._get(path)
        if not isinstance(payload, dict) or not payload.get("content"):
            return None
        try:
            return base64.b64decode(payload["content"]).decode("utf-8", "replace")
        except Exception:                                  # noqa: BLE001 -- not text, not a rule's
            return None


def _materialise(tree: _Tree, into: str) -> bool:
    """Write enough of the repository for the rules to read. -> whether anything was found."""
    root = set(tree.names())
    if not root:
        return False
    for name in _MANIFESTS:
        if name not in root:
            continue
        body = tree.text(name)
        if body is None:
            continue
        with open(os.path.join(into, name), "w", encoding="utf-8") as handle:
            handle.write(body)
    for relative in _LISTINGS:
        head = relative.split("/", 1)[0]
        if head not in root:
            continue
        entries = tree.names(relative)
        if not entries:
            continue
        os.makedirs(os.path.join(into, relative), exist_ok=True)
        for entry in entries:
            # A NAME IS ENOUGH FOR A LISTING RULE, and the contents are not. `cmd/<name>/main.go`
            # is a directory holding a file; the rule looks for the file, so the file is created
            # empty rather than downloaded.
            if relative == "cmd":
                os.makedirs(os.path.join(into, relative, entry), exist_ok=True)
                open(os.path.join(into, relative, entry, "main.go"), "w").close()
            else:
                open(os.path.join(into, relative, entry), "w").close()
    for relative in _NESTED:
        head = relative.split("/", 1)[0]
        if head not in root:
            continue
        if os.path.exists(os.path.join(into, relative)):
            continue
        body = tree.text(relative)
        if body is None:
            continue
        os.makedirs(os.path.dirname(os.path.join(into, relative)), exist_ok=True)
        with open(os.path.join(into, relative), "w", encoding="utf-8") as handle:
            handle.write(body)
    for name in root:
        if name.endswith(".gemspec"):
            open(os.path.join(into, name), "w").close()
    return True


def declares_a_command(http, full_name: str, ref: str = "") -> bool | None:
    """Whether this repository declares a runnable entry point. -> True / False / None.

    None means UNANSWERED -- the API declined, the repository is private, the request budget ran
    out. A caller must not read that as "no": refusing on an unanswered question would silently
    narrow the supply whenever GitHub was having a bad minute.
    """
    from ..scales.repo import _discover_entrypoint

    tree = _Tree(http, full_name, ref)
    scratch = tempfile.mkdtemp(prefix="frf-runnable-")
    try:
        if not _materialise(tree, scratch):
            return None
        try:
            _discover_entrypoint(scratch)
        except ValueError:
            return False
        except Exception:                                  # noqa: BLE001 -- ours, so do not refuse
            return None
        return True
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


__all__ = ["declares_a_command", "MAX_REQUESTS"]
