"""The indexes candidates are sourced from -- one per registry, plus code search.

`core/sourcing.py` states the rule these exist to satisfy: a candidate list must come from an
enumerable index, and a model may rank or filter candidates but never produce them. This package is
what makes that rule affordable, because an index is only useful if someone has written the fifty
lines that page it.

WHAT EVERY CLIENT IN HERE PROMISES.

    page(n, size=...)   the nth page, as Candidates. EMPTY MEANS EXHAUSTED and nothing else does:
                        a network failure raises. That asymmetry is the one thing in this package
                        that must not be got wrong -- `walk()` stops on an empty page, so a client
                        that turned a timeout into `[]` would end a batch early and write a
                        coverage record saying the supply had run out.
    total()             how many exist, or None when the registry will not say. None is honest and
                        supported; a fabricated denominator is not.
    name                what appears in the coverage log.

WHICH REGISTRIES ANSWER WHICH QUESTION HONESTLY. They differ more than one would hope, and the
differences are recorded in each module rather than smoothed over:

    pypi         a full name list, paged locally. A real total.
    npm          server-side paging over a search. A real total for that search.
    crates.io    real paging and a real total; page 1001 answers 400, so `reachable()` says what
                 can actually be walked as distinct from what exists.
    pkg.go.dev   the module proxy's index, walked by publication time.
    maven        server-side paging over a search, with a real total.
    rubygems     paged, but publishes NO total. `total()` returns None and means it.
    github       repository search, capped at 1000 results per query -- so it is SEGMENTED by star
                 range, which is the only way past a wall that low.

`filters` holds the mechanical prefilters DESIGN.md s10 lists. They are decidable from registry
metadata alone: no model, no judgement, and each names which check refused so that a fall in yield
can be attributed rather than guessed at.
"""
from __future__ import annotations

from .crates import Crates
from .filters import Facts, accepts, keep, refusals
from .github import GitHub
from .golang import GoModules
from .http import (Http, HttpError, NotFound, RateLimited, SourceError, TransportError,
                   Unauthorized)
from .maven import MavenCentral
from .npm import Npm
from .pypi import PyPI
from .rubygems import RubyGems

# Registry name -> the client that walks it. A table so that "which indexes does this installation
# have" is a question with a printable answer, in the same spirit as the shim and coverage tables.
INDEXES = {
    "pypi": PyPI,
    "npm": Npm,
    "crates.io": Crates,
    "pkg.go.dev": GoModules,
    "maven": MavenCentral,
    "rubygems": RubyGems,
    "github": GitHub,
}


def available() -> list[str]:
    """Which indexes this installation can source from."""
    return sorted(INDEXES)


def index_for(name: str):
    """-> the client class for one registry.

    Raises rather than returning None, and names what there is. A caller that mistyped a registry
    would otherwise get an AttributeError somewhere that has forgotten what it asked for.
    """
    key = (name or "").strip().lower()
    if key not in INDEXES:
        raise LookupError("no index called %r; this installation has %s"
                          % (name, ", ".join(available())))
    return INDEXES[key]


__all__ = ["Crates", "Facts", "GitHub", "GoModules", "Http", "HttpError", "INDEXES",
           "MavenCentral", "NotFound", "Npm", "PyPI", "RateLimited", "RubyGems", "SourceError",
           "TransportError", "Unauthorized", "accepts", "available", "index_for", "keep",
           "refusals"]
