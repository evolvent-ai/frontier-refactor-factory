"""OCaml packages from the opam repository (opam.ocaml.org), for the package scale.

PACKAGES COME FROM THE OPAM REPOSITORY INDEX. The official opam package repository at
github.com/ocaml/opam-repository stores packages as directories under packages/; the
GitHub contents API can list that tree and gives us a stable, paged, countable set of
names -- the same shape as PyPI's simple index approach.

EACH PACKAGE IS A DIRECTORY containing version subdirectories. The top-level listing
gives names and is the snapshot; the latest version is retrieved from the per-package
metadata if hydration is on.

GITHUB TOKEN IS OPTIONAL but raises the GitHub API rate limit from 60 to 5000 req/hour
when provided. It is read through `core.credentials` (never directly from `os.environ`)
and falls back to unauthenticated gracefully.

WHAT IS NOT DONE: resolving depext (system dependencies) or opam's solve-time dependency
expansion. The depends field in the opam file counts direct declared dependencies, which
is what the closure filter needs and is the honest number to report.
"""
from __future__ import annotations

from ..core import credentials
from ..core.scale import Candidate
from . import filters
from .http import Http, NotFound, all_or_nothing

# The opam package repository on GitHub. Each entry in the packages/ tree is a package.
TREE = "https://api.github.com/repos/ocaml/opam-repository/contents/packages"
# Per-package contents to find version subdirectories.
PKG_TREE = "https://api.github.com/repos/ocaml/opam-repository/contents/packages/%s"
# The opam file for a specific version.
OPAM_FILE = ("https://raw.githubusercontent.com/ocaml/opam-repository/master/"
             "packages/%s/%s.%s/opam")

LANGUAGE = "ocaml"

# GitHub answers at most 1000 entries per directory listing, so we snapshot the list
# and page locally -- the same pattern as PyPI.
MAX_ENTRIES = 1000


class Opam:
    """OCaml packages from the opam repository, paged from a local snapshot."""

    name = "opam"

    def __init__(self, http: Http | None = None, *, subset: str = "",
                 scale: str = "package", hydrate: bool = False) -> None:
        token = credentials.get("GITHUB_TOKEN") or ""
        self._http = http or Http(token=token)
        self._subset = subset.strip().lower()
        self._scale = scale
        self._hydrate = hydrate
        self._names: list[str] | None = None

    def total(self) -> int | None:
        return len(self._snapshot())

    def page(self, number: int, *, size: int = 20):
        names = self._snapshot()
        window = names[number * size:(number + 1) * size]
        if not window:
            return []
        return all_or_nothing(window, self._candidate, index=self.name)

    def _snapshot(self) -> list[str]:
        if self._names is not None:
            return self._names
        entries = self._http.json(TREE, accept="application/vnd.github+json")
        if not isinstance(entries, list):
            entries = []
        names = sorted(e["name"] for e in entries
                       if isinstance(e, dict) and e.get("type") == "dir" and e.get("name"))
        if self._subset:
            names = [n for n in names if self._subset in n.lower()]
        self._names = names
        return names

    def _candidate(self, name: str) -> Candidate:
        version = ""
        description = ""
        dependencies: int | None = None
        if self._hydrate:
            version, description, dependencies = self._fetch_detail(name)
        else:
            version = self._latest_version(name)
        if not version:
            raise NotFound("opam: %s: could not determine a version" % name,
                           status=404, url=PKG_TREE % name)
        return to_candidate(name, version, description=description,
                            dependencies=dependencies, scale=self._scale)

    def _latest_version(self, name: str) -> str:
        """The most recent version subdirectory for a package, or "" when none found."""
        try:
            entries = self._http.json(PKG_TREE % name, accept="application/vnd.github+json")
        except NotFound:
            return ""
        if not isinstance(entries, list):
            return ""
        # Version directories are named "<package>.<version>". Take the last alphabetically.
        dirs = sorted(e["name"] for e in entries
                      if isinstance(e, dict) and e.get("type") == "dir" and e.get("name"))
        if not dirs:
            return ""
        # Strip the package name prefix: "pkg.1.2.3" -> "1.2.3"
        last = dirs[-1]
        prefix = name + "."
        return last[len(prefix):] if last.startswith(prefix) else last

    def _fetch_detail(self, name: str) -> tuple:
        version = self._latest_version(name)
        if not version:
            return "", "", None
        try:
            raw = self._http.get(OPAM_FILE % (name, name, version)).decode("utf-8", "replace")
        except NotFound:
            return version, "", None
        description = _parse_synopsis(raw)
        dependencies = _parse_depends(raw)
        return version, description, dependencies


def _parse_synopsis(opam_text: str) -> str:
    for line in opam_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("synopsis:"):
            value = stripped[len("synopsis:"):].strip().strip('"')
            return value
    return ""


def _parse_depends(opam_text: str) -> int | None:
    """Count direct depends entries. Stops counting at the closing bracket."""
    count = 0
    in_depends = False
    for line in opam_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("depends:"):
            in_depends = True
            continue
        if in_depends:
            if stripped == "]":
                break
            if stripped and not stripped.startswith("#"):
                count += 1
    return count if in_depends else None


def to_candidate(name: str, version: str, *, description: str = "",
                 dependencies: int | None = None, scale: str = "package",
                 source: str = "opam") -> Candidate:
    """An opam package name and version -> a Candidate."""
    facts = filters.Facts(
        name=name, version=version, summary=description or name.replace("-", " "),
        keywords=tuple(name.split("-")),
        dependencies=dependencies, has_tests=None,
        repository="https://opam.ocaml.org/packages/%s" % name,
        documented=True)
    return Candidate(
        identity="opam:%s@%s" % (name, version),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": name, "version": version,
            "description": description or "the OCaml package %s at %s" % (name, version),
            "entry_points": [], "root": "",
            "install": ["opam", "install", "%s.%s" % (name, version)],
            "forbidden": [name],
            "repository": "https://opam.ocaml.org/packages/%s" % name,
            "facts": filters.as_json(facts),
        })
