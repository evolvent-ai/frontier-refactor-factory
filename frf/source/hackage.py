"""Haskell packages from Hackage (hackage.haskell.org), for the package scale.

ALL NAMES, PAGED LOCALLY. Hackage publishes a complete list of every package name at
/packages/names -- one name per line, stable alphabetical order. That endpoint is the
equivalent of PyPI's simple index: a real snapshot, a real total, and stable paging
without asking the server twice.

PER-PACKAGE METADATA IS A SECOND FETCH. The names endpoint carries no description,
no version, no dependencies. Each name on a page costs one JSON request to
/package/<name>/preferred to get the preferred version, and /package/<name>/<version>.json
to get the cabal metadata. So hydration is opt-in and off by default, for the same reason
it is in pypi.py: a page of fifty names at a time means fifty extra requests.

WITH HYDRATION OFF the version is the latest stable release (from /preferred), and
description and dependencies are reported as unknown. That is honest and usable: the
build stage will have the real cabal file.
"""
from __future__ import annotations

from ..core.scale import Candidate
from . import filters
from .http import Http, NotFound, all_or_nothing

NAMES = "https://hackage.haskell.org/packages/names"
PREFERRED = "https://hackage.haskell.org/package/%s/preferred"
INFO = "https://hackage.haskell.org/package/%s-%s.json"

LANGUAGE = "haskell"


class Hackage:
    """All Haskell packages on Hackage, paged from a local snapshot of the name list."""

    name = "hackage"

    def __init__(self, http: Http | None = None, *, subset: str = "",
                 scale: str = "package", hydrate: bool = False) -> None:
        self._http = http or Http()
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
        # The /packages/names endpoint returns one package name per line, plain text.
        lines = self._http.lines(NAMES)
        names = sorted(set(line.strip() for line in lines if line.strip()))
        if self._subset:
            names = [n for n in names if self._subset in n.lower()]
        self._names = names
        return names

    def _candidate(self, name: str) -> Candidate:
        # Fetch the preferred version first; that gives us the pinned release identifier.
        try:
            preferred = self._http.json(PREFERRED % name)
        except NotFound:
            raise NotFound("hackage: %s has no preferred version" % name,
                           status=404, url=PREFERRED % name)
        versions = preferred.get("normal-version") or []
        version = str(versions[0]) if versions else ""
        if not version:
            raise NotFound("hackage: %s: no normal version" % name,
                           status=404, url=PREFERRED % name)
        description = ""
        dependencies: int | None = None
        if self._hydrate:
            try:
                info = self._http.json(INFO % (name, version))
                description = str(info.get("synopsis") or info.get("description") or "")
                deps = info.get("dependencies") or {}
                dependencies = len(deps) if isinstance(deps, dict) else None
            except NotFound:
                pass
        return to_candidate(name, version, description=description,
                            dependencies=dependencies, scale=self._scale)


def to_candidate(name: str, version: str, *, description: str = "",
                 dependencies: int | None = None, scale: str = "package",
                 source: str = "hackage") -> Candidate:
    """A Hackage package name and version -> a Candidate."""
    facts = filters.Facts(
        name=name, version=version, summary=description or name.replace("-", " "),
        keywords=tuple(name.split("-")),
        dependencies=dependencies, has_tests=None,
        repository="https://hackage.haskell.org/package/%s" % name,
        documented=True)
    return Candidate(
        identity="hackage:%s@%s" % (name, version),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": name, "version": version,
            "description": description or "the Haskell package %s at %s" % (name, version),
            "entry_points": [], "root": "",
            "install": ["cabal", "install", "%s-%s" % (name, version)],
            "forbidden": [name],
            "repository": "https://hackage.haskell.org/package/%s" % name,
            "facts": filters.as_json(facts),
        })
