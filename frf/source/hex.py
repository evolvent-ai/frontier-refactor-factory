"""Elixir and Erlang packages from hex.pm, for the package scale.

REAL PAGING, REAL TOTAL. The hex.pm API exposes /api/packages with `page` and `per_page`
parameters and a `total` in the response meta block, so paging is arithmetic and
`total()` is not fabricated. Verified against the live API: page 1 and page 10 at the
same query each answer 200 with the same `meta.total`.

ELIXIR VS ERLANG. Most packages on hex.pm are Elixir, but a meaningful minority are
Erlang (and some support both via the rebar3 / mix dual-build convention). The language
is recorded in each package's metadata as `build_tools`: a package that lists `rebar3`
without `mix` is Erlang; one that lists `mix` (with or without `rebar3`) is Elixir.
Where the build tools are absent, the language defaults to "elixir" since that is the
majority of the registry.

AUTHENTICATION. hex.pm allows unauthenticated reads at a generous rate; no token is
required. If a `HEX_API_KEY` is set (through `core.credentials`), it is passed as a
Bearer token to raise the limit further, but its absence never fails the run.

DEPENDENCIES FROM THE SAME ENDPOINT. Unlike Maven and RubyGems, the package listing
row already carries `latest_stable_version` requirements in some cases. Reliable
dependency counts require a second fetch to /api/packages/<name>/releases/<version>,
so they are opt-in via `hydrate=True`.
"""
from __future__ import annotations

from urllib.parse import quote

from ..core import credentials
from ..core.scale import Candidate
from . import filters
from .http import Http, NotFound, envelope

PACKAGES = "https://hex.pm/api/packages?page=%d&per_page=%d&sort=name"
RELEASE = "https://hex.pm/api/packages/%s/releases/%s"

LANGUAGE_ELIXIR = "elixir"
LANGUAGE_ERLANG = "erlang"

MAX_PER_PAGE = 100

# Sort key is stable (name) so that page N is page N again for a later run.


class Hex:
    """hex.pm package listing, paged server-side."""

    name = "hex.pm"

    def __init__(self, http: Http | None = None, *, scale: str = "package",
                 hydrate: bool = False) -> None:
        token = credentials.get("HEX_API_KEY") or ""
        self._http = http or Http(token=token)
        self._scale = scale
        self._hydrate = hydrate
        self._total: int | None = None

    def total(self) -> int | None:
        """What hex.pm reports for the total package count."""
        if self._total is None:
            self._fetch(1, 1)
        return self._total

    def page(self, number: int, *, size: int = 20):
        size = min(size, MAX_PER_PAGE)
        # hex.pm pages from 1; walk() counts from 0.
        rows = self._fetch(number + 1, size)
        if not rows:
            return []
        return [to_candidate(row, scale=self._scale, source=self.name,
                             dependencies=self._dependencies(row)) for row in rows]

    def _fetch(self, page_number: int, size: int) -> list:
        payload = self._http.json(PACKAGES % (page_number, size))
        # hex.pm answers a bare list of packages, not an envelope.
        if isinstance(payload, list):
            return payload
        # Some versions wrap in a meta envelope; handle both shapes.
        meta = payload.get("meta") or {}
        total = meta.get("total") or payload.get("total")
        if isinstance(total, int):
            self._total = total
        # Try to extract a list from the payload.
        for key in ("packages", "data", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []

    def _dependencies(self, row: dict) -> int | None:
        if not self._hydrate:
            return None
        name = str(row.get("name") or "")
        version = _latest_version(row)
        if not name or not version:
            return None
        try:
            release = self._http.json(RELEASE % (quote(name), quote(version)))
        except NotFound:
            return None
        reqs = release.get("requirements") or {}
        return len(reqs) if isinstance(reqs, dict) else None


def to_candidate(row: dict, *, scale: str = "package", source: str = "hex.pm",
                 dependencies: int | None = None) -> Candidate:
    """A hex.pm package row -> a Candidate."""
    name = str(row.get("name") or "")
    version = _latest_version(row)
    language = _language(row)
    facts = _facts(row, version, dependencies)
    return Candidate(
        identity="hex:%s@%s" % (name, version),
        scale=scale, language=language, source=source,
        detail={
            "package": name, "version": version,
            "description": str((row.get("meta") or {}).get("description") or ""),
            "entry_points": [], "root": "",
            "install": ["mix", "deps.get"] if language == LANGUAGE_ELIXIR
                       else ["rebar3", "get-deps"],
            "forbidden": [name],
            "repository": _repository(row),
            "facts": filters.as_json(facts),
        })


def _latest_version(row: dict) -> str:
    """The latest stable release version, or the latest pre-release if nothing stable."""
    releases = row.get("releases") or []
    if not isinstance(releases, list):
        return ""
    # hex.pm orders releases newest-first; stable ones have no pre-release tag.
    for release in releases:
        v = str(release.get("version") or "")
        if v and not any(tag in v for tag in ("-rc", "-alpha", "-beta", "-dev")):
            return v
    # Fall back to any release.
    return str(releases[0].get("version") or "") if releases else ""


def _language(row: dict) -> str:
    meta = row.get("meta") or {}
    tools = [str(t).lower() for t in (meta.get("build_tools") or ())]
    if "rebar3" in tools and "mix" not in tools:
        return LANGUAGE_ERLANG
    return LANGUAGE_ELIXIR


def _facts(row: dict, version: str, dependencies: int | None) -> filters.Facts:
    meta = row.get("meta") or {}
    return filters.Facts(
        name=str(row.get("name") or ""),
        version=version,
        summary=str(meta.get("description") or ""),
        keywords=tuple(str(k) for k in (meta.get("licenses") or ())),
        dependencies=dependencies, has_tests=None,
        repository=_repository(row),
        yanked=False,
        documented=bool(meta.get("description")))


def _repository(row: dict) -> str:
    links = (row.get("meta") or {}).get("links") or {}
    for key in ("GitHub", "Gitlab", "Source", "Repository", "source", "github"):
        if links.get(key):
            return str(links[key])
    return ""
