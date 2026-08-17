"""crates.io, for the package scale.

THE BEST-BEHAVED OF THE SIX. Real server-side paging (`page` and `per_page`), a real total in
`meta.total`, and a documented sort key -- so `sort=alpha` gives an order that does not depend on
download counts moving, which is what "stable between pages" actually requires.

ONE MEASURED LIMIT, and it is a hard one: `page` above 1000 answers 400. Verified live -- page 100
at 100 per page returns rows, page 1001 is a Bad Request. So at most 100,000 crates are reachable
through this endpoint out of the ~318,000 the total reports. `total()` reports what crates.io says
because that is the truth about the registry; `reachable()` below says what paging can actually get
to. Reporting only the first would overstate the supply and only the second would understate the
registry, so both exist and the docstrings say which is which.

crates.io asks API clients for a descriptive User-Agent and rate-limits ones without it. That is
handled in http.py for every client here, and this is the registry that made it non-optional.
"""
from __future__ import annotations

from urllib.parse import quote

from ..core.scale import Candidate
from . import filters
from .http import Http

LIST = "https://crates.io/api/v1/crates?page=%d&per_page=%d&sort=%s"
CRATE = "https://crates.io/api/v1/crates/%s"

LANGUAGE = "rust"

MAX_PAGE = 1000                 # measured: 1001 answers 400
MAX_PER_PAGE = 100              # documented, and 100 is what the API accepts

# `alpha` rather than `downloads`: alphabetical order does not change between pages, and a download
# ranking does. The sourcing rule's second half -- an index must not reorder itself between pages --
# is a choice made here rather than a property of the API.
DEFAULT_SORT = "alpha"


class Crates:
    """crates.io's crate list, paged server-side."""

    name = "crates.io"

    def __init__(self, http: Http | None = None, *, sort: str = DEFAULT_SORT,
                 query: str = "", scale: str = "package", hydrate: bool = False) -> None:
        self._http = http or Http()
        self._sort = sort
        self._query = query
        self._scale = scale
        # The list endpoint already carries description, keywords, categories and the max stable
        # version, which is four of the five filters. Only the dependency count needs a second
        # request, so hydration is off by default here and the closure is reported unknown.
        self._hydrate = hydrate
        self._total: int | None = None

    def total(self) -> int | None:
        """What crates.io reports the registry holds. See `reachable()` for what paging can get to."""
        if self._total is None:
            self._fetch(1, 1)
        return self._total

    def reachable(self) -> int | None:
        """How many of those this endpoint will actually serve, given the page-1000 ceiling.

        Separate from `total()` on purpose. Coverage accounting divides by the total, and a supply
        that reports 318,000 while serving 100,000 would show a run stalling at 31% with no
        explanation -- the explanation being an API limit rather than anything about the material.
        """
        total = self.total()
        return None if total is None else min(total, MAX_PAGE * MAX_PER_PAGE)

    def page(self, number: int, *, size: int = 20):
        size = min(size, MAX_PER_PAGE)
        # crates.io pages from 1; walk() counts from 0.
        page_number = number + 1
        if page_number > MAX_PAGE:
            # Exhausted as far as this endpoint is concerned, which is what an empty page means. It
            # is NOT a failure -- the API is answering correctly and we have reached its ceiling --
            # so it returns empty rather than raising, and `reachable()` is what documents the wall.
            return []
        return [to_candidate(row, scale=self._scale, source=self.name,
                             dependencies=self._dependencies(row))
                for row in self._fetch(page_number, size)]

    def _fetch(self, page_number: int, size: int) -> list:
        url = LIST % (page_number, size, quote(self._sort))
        if self._query:
            url += "&q=%s" % quote(self._query)
        payload = self._http.json(url)
        total = (payload.get("meta") or {}).get("total")
        if isinstance(total, int):
            self._total = total
        return list(payload.get("crates") or ())

    def _dependencies(self, row: dict) -> int | None:
        if not self._hydrate:
            return None
        name = quote(str(row.get("id") or row.get("name") or ""))
        payload = self._http.json("%s/dependencies" % (CRATE % name))
        # Only what a plain build needs. Dev-dependencies are the crate's test harness, and counting
        # them would reject well-tested crates for being well tested.
        return len([d for d in (payload.get("dependencies") or ())
                    if str(d.get("kind") or "normal") == "normal" and not d.get("optional")])


def to_candidate(row: dict, *, scale: str = "package", source: str = "crates.io",
                 dependencies: int | None = None) -> Candidate:
    """A crates.io list row -> a Candidate."""
    name = str(row.get("id") or row.get("name") or "")
    version = str(row.get("max_stable_version") or row.get("newest_version")
                  or row.get("max_version") or "")
    facts = _facts(row, version, dependencies)
    return Candidate(
        identity="crates:%s@%s" % (name, version),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": name, "version": version,
            "description": str(row.get("description") or ""),
            "entry_points": [], "root": "",
            # `cargo add` rather than a manifest edit: it resolves and pins in one step, and the
            # container has to do this offline from a vendored registry either way.
            "install": ["cargo", "add", "%s@%s" % (name, version)],
            "forbidden": [name, name.replace("-", "_")],
            "repository": facts.repository,
            "facts": filters.as_json(facts),
        })


def _facts(row: dict, version: str, dependencies: int | None) -> filters.Facts:
    return filters.Facts(
        name=str(row.get("id") or row.get("name") or ""),
        version=version,
        summary=str(row.get("description") or ""),
        # Categories join the keywords: crates.io's category vocabulary (`algorithms`,
        # `data-structures`, `network-programming`) is exactly the signal the I/O and compute markers
        # look for, and it is more reliably populated than free-text keywords.
        keywords=tuple([str(k) for k in (row.get("keywords") or ())]
                       + [str(c) for c in (row.get("categories") or ())]),
        dependencies=dependencies,
        # crates.io publishes no test metadata. None, not False -- see filters.has_own_tests for why
        # that distinction keeps a whole ecosystem in the supply.
        has_tests=None,
        repository=str(row.get("repository") or row.get("homepage") or ""),
        yanked=bool(row.get("yanked")),
        documented=bool(row.get("documentation") or row.get("description")))
