"""npm, for the package scale.

GENUINELY SERVER-PAGED, with a caveat worth stating. The registry's search endpoint takes `from` and
`size` and returns a `total`, so paging is arithmetic on the server side and `total()` is the number
npm itself reports. Verified live: `from=0`, `from=5000` and `from=14000` against the same query all
answer 200 with the same `total`, so there is no deep-paging cliff of the kind GitHub has.

THE CAVEAT. `total` is the size of the RESULT SET for a query, not of the registry, and npm's
ranking is a scored blend that can shift as download counts move. Two pages fetched a week apart may
therefore not tile perfectly. The mitigation is the one that matters for this factory: `identity`
carries the version, so a package that reappears under a shifted rank is recognised by `Memory` and
counted as a repeat rather than rebuilt. A drifting rank costs a little duplicate work; it does not
corrupt the accounting, which is what would actually be unrecoverable.

WHY A QUERY IS REQUIRED. There is no "all packages" endpoint that pages -- the replication feed is a
different shape entirely and is not a search index. So this index is scoped by a keyword, and its
`total()` is honest about being the total for that scope. Several scopes are a several indexes, each
countable, which is what the coverage accounting wants anyway.
"""
from __future__ import annotations

from urllib.parse import quote

from ..core.scale import Candidate
from . import filters
from .http import Http, all_or_nothing, envelope

SEARCH = "https://registry.npmjs.org/-/v1/search?text=%s&size=%d&from=%d"
PACKUMENT = "https://registry.npmjs.org/%s"

LANGUAGE = "javascript"

# npm's search caps a page at 250. Asking for more is not an error, it is silently truncated, which
# would make `page(n, size=500)` skip half the index without saying so.
MAX_PAGE = 250


class Npm:
    """One npm search scope, paged server-side.

    `query` is npm's own search syntax -- `keywords:algorithm`, `parser`, and so on. It scopes the
    index; it does not produce names, which the registry does.
    """

    name = "npm"

    def __init__(self, http: Http | None = None, *, query: str = "keywords:algorithm",
                 scale: str = "package", hydrate: bool = True) -> None:
        self._http = http or Http()
        self._query = query
        self._scale = scale
        # Whether to fetch the full packument per package. Search results carry no dependency list,
        # so the closure filter cannot decide without it. Off is faster and reports `dependencies`
        # as unknown, which `has_small_closure` accepts -- an explicit trade, not a silent one.
        self._hydrate = hydrate
        self._total: int | None = None

    def total(self) -> int | None:
        """What npm says the result set holds, or None if it has not been asked yet and will not say.

        Populated as a side effect of the first page rather than by a probe request, so that walking
        an index costs one request per page and not two.
        """
        if self._total is None:
            self._fetch(0, 1)
        return self._total

    def page(self, number: int, *, size: int = 20):
        rows = self._fetch(number * min(size, MAX_PAGE), min(size, MAX_PAGE))
        if not rows:
            return []
        return all_or_nothing(rows, self._candidate, index=self.name)

    def _fetch(self, offset: int, size: int) -> list:
        payload = self._http.json(SEARCH % (quote(self._query, safe=":"), size, offset))
        total = payload.get("total")
        if isinstance(total, int):
            self._total = total
        return [row.get("package") or {}
                for row in envelope(payload, "objects", index=self.name)]

    def _candidate(self, row: dict) -> Candidate:
        version = {}
        if self._hydrate:
            packument = self._http.json(PACKUMENT % quote(str(row.get("name", "")), safe="@/"))
            tag = (packument.get("dist-tags") or {}).get("latest", "")
            version = (packument.get("versions") or {}).get(tag) or {}
        return to_candidate(row, version, scale=self._scale, source=self.name)


def to_candidate(row: dict, version: dict | None = None, *, scale: str = "package",
                 source: str = "npm") -> Candidate:
    """A search row (+ optionally the packument's latest version) -> a Candidate."""
    version = version or {}
    name = str(row.get("name") or version.get("name") or "")
    number = str(row.get("version") or version.get("version") or "")
    facts = _facts(row, version)
    return Candidate(
        identity="npm:%s@%s" % (name, number),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": name, "version": number,
            "description": str(row.get("description") or version.get("description") or ""),
            "entry_points": [], "root": "",
            "install": ["npm", "install", "%s@%s" % (name, number)],
            "forbidden": [name],
            "repository": facts.repository,
            "facts": filters.as_json(facts),
        })


def _facts(row: dict, version: dict) -> filters.Facts:
    scripts = version.get("scripts") or {}
    dependencies = version.get("dependencies")
    links = row.get("links") or {}
    return filters.Facts(
        name=str(row.get("name") or version.get("name") or ""),
        version=str(row.get("version") or version.get("version") or ""),
        summary=str(row.get("description") or version.get("description") or ""),
        keywords=tuple(str(k) for k in (row.get("keywords") or version.get("keywords") or ())),
        # None when the packument was not fetched. Unknown is not zero, and reporting zero here
        # would let a package with forty dependencies through the closure filter.
        dependencies=len(dependencies) if isinstance(dependencies, dict) else None,
        # A `test` script that is npm's own "no test specified" placeholder is not a test suite.
        has_tests=_has_test_script(scripts) if scripts else None,
        repository=_repository(row, version, links),
        documented=bool(links.get("homepage") or version.get("homepage")))


def _has_test_script(scripts: dict) -> bool:
    script = str(scripts.get("test") or "")
    return bool(script) and "no test specified" not in script.lower()


def _repository(row: dict, version: dict, links: dict) -> str:
    repository = version.get("repository")
    if isinstance(repository, dict):
        return str(repository.get("url") or "")
    if isinstance(repository, str):
        return repository
    return str(links.get("repository") or row.get("links", {}).get("repository") or "")
