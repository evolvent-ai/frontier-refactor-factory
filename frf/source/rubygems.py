"""RubyGems, for the package scale.

PAGED, BUT NOT COUNTED. The search endpoint takes a `page` and returns thirty rows at a time, and it
publishes no total anywhere -- so `total()` returns None. That is the honest answer and the design
supports it: `Coverage.remaining` becomes unknown rather than wrong, and a yield computed here is a
yield against a denominator nobody claims to know. Inventing one from the number of pages walked so
far would be worse than None, because it would look like a measurement.

THE SEARCH IS THE ONLY ENUMERATION. RubyGems has no "list every gem" API. What it has is a query,
which means the index is a SLICE of the registry chosen by a search term rather than the registry
itself -- and the slice is stable, which is what the sourcing rule actually requires. The term is a
constructor argument so that a run can say which slice it walked, and the identity records it.

DEPENDENCIES COST A SECOND REQUEST. The search rows carry description, version and repository, which
is four of the five filters; the dependency list is only on the per-gem endpoint. Hydration is
therefore optional, and with it off the closure is reported unknown rather than guessed -- which the
filters accept, because the build stage finds out for certain.
"""
from __future__ import annotations

from urllib.parse import quote

from ..core.scale import Candidate
from . import filters
from .http import Http, all_or_nothing

SEARCH = "https://rubygems.org/api/v1/search.json?query=%s&page=%d"
GEM = "https://rubygems.org/api/v1/gems/%s.json"

LANGUAGE = "ruby"

# What the search endpoint returns per page. Not configurable at the API, so a caller asking for a
# different size gets what RubyGems gives; saying so here is better than accepting a `size` that
# quietly does nothing.
PAGE_SIZE = 30


class RubyGems:
    """RubyGems' search, paged. The slice is chosen by the query and recorded in every identity."""

    name = "rubygems"

    def __init__(self, http: Http | None = None, *, query: str = "algorithm",
                 scale: str = "package", hydrate: bool = False) -> None:
        self._http = http or Http()
        self._query = query
        self._scale = scale
        self._hydrate = hydrate

    def total(self) -> int | None:
        """None, and deliberately.

        RubyGems publishes no count for a search. Reporting the registry's overall gem count would
        be a different number than this index can reach, and reporting pages-walked-so-far would be
        a measurement of this run rather than of the supply.
        """
        return None

    def page(self, number: int, *, size: int = PAGE_SIZE):
        """One page of gems. Empty means exhausted; anything else raises.

        `size` is accepted for the Index protocol and cannot be honoured -- the endpoint's page is
        thirty rows. A caller asking for more gets thirty, and gets more pages, which is the same
        material at a different granularity.
        """
        rows = self._http.json(SEARCH % (quote(self._query), number + 1))
        if not isinstance(rows, list):
            return []
        rows = rows[:size] if size and size < len(rows) else rows
        if not self._hydrate:
            return [self._candidate(row, None) for row in rows]
        return all_or_nothing(rows, lambda row: self._candidate(row, self._detail(row)),
                              index=self.name)

    def _detail(self, row: dict) -> dict:
        return self._http.json(GEM % quote(str(row.get("name", ""))))

    def _candidate(self, row: dict, detail: dict | None) -> Candidate:
        return to_candidate(row, detail, scale=self._scale, query=self._query)


def to_candidate(row: dict, detail: dict | None = None, *, scale: str = "package",
                 source: str = "rubygems", query: str = "") -> Candidate:
    """One search row -> a Candidate.

    Separate from the client so that the parsing can be tested against a recorded row with no
    network at all -- which is the half that breaks when a registry renames a field.
    """
    name = str(row.get("name") or "")
    version = str(row.get("version") or "")
    facts = _facts(row, detail)
    return Candidate(
        # The query is part of the identity because it is part of what was walked. Two runs over
        # different slices that both reach the same gem should agree it is the same gem, so the
        # query is recorded in `detail` rather than in the identity itself.
        identity="gem:%s@%s" % (name, version),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": name, "version": version,
            "description": str(row.get("info") or ""),
            "entry_points": [], "root": "",
            "install": ["gem", "install", name, "--version", version],
            # The gem is what a submission must not reach for. Naming it here is what lets the
            # delegation check inspect for it without the scale having to know which registry the
            # candidate came from.
            "forbidden": [name],
            "repository": facts.repository,
            "slice": query,
            "facts": filters.as_json(facts),
        })


def _facts(row: dict, detail: dict | None) -> filters.Facts:
    metadata = row.get("metadata") or {}
    return filters.Facts(
        name=str(row.get("name") or ""),
        version=str(row.get("version") or ""),
        summary=str(row.get("info") or ""),
        # RubyGems publishes no keywords. The licences are the only other short text field and they
        # say nothing about what the gem does, so the haystack is the name and description alone --
        # which is stated here because an empty tuple looks like an oversight otherwise.
        keywords=(),
        dependencies=_dependencies(detail),
        # Not published for a gem. None rather than False: the filters accept unknown and reject
        # known-absent, and a registry that does not say has not said no.
        has_tests=None,
        repository=str(metadata.get("source_code_uri") or row.get("source_code_uri")
                       or row.get("homepage_uri") or ""),
        yanked=False,
        documented=bool(row.get("documentation_uri")),
        extra={"platform": str(row.get("platform") or ""),
               "licenses": list(row.get("licenses") or ())})


def _dependencies(detail: dict | None) -> int | None:
    """How many gems it needs at run time, or None when nobody asked.

    Development dependencies are excluded on purpose: they are what the gem's own authors need in
    order to test it, not what an offline container must install to use it, and counting them would
    reject well-behaved gems for having a thorough test suite.
    """
    if not detail:
        return None
    dependencies = (detail.get("dependencies") or {}).get("runtime")
    return len(dependencies) if isinstance(dependencies, list) else None
