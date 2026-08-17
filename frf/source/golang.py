"""Go modules, via index.golang.org and proxy.golang.org, for the package scale.

CURSOR-PAGED, NOT OFFSET-PAGED, and this is the client where that distinction has teeth. The module
index answers "everything published since timestamp T", newline-delimited JSON, up to a limit. There
is no page number. So `page(n)` is implemented over a cursor the client remembers: page 0 starts at
`since`, and each page's last timestamp becomes the next page's start.

WHAT THAT COSTS AND WHY IT IS STILL AN INDEX. Random access is not free -- asking for page 5 without
having fetched 0 through 4 means fetching them, because the only way to know page 5's cursor is to
walk there. `walk()` pages sequentially so this never bites in practice, and the cursors are cached
so that re-asking for a page already seen replays it instead of refetching. What matters for the
sourcing rule is the other half: the index is stably ordered. Verified live -- the same `since`
fetched twice returns byte-identical rows, and a window starting at the previous window's last
timestamp begins with that same module. So page N is page N again, which is the requirement.

THERE IS NO TOTAL, AND NONE IS INVENTED. The index publishes a stream ordered by publication time
with no count, no "X of Y", and no last page. `total()` returns None. It would have been easy to
divide a date range by an average rate and call it an estimate; that number would then be a
denominator in a coverage report, and every yield computed against it would be fiction.

WHY THE PROXY AS WELL AS THE INDEX. The index gives a path and a version and nothing else -- no
description, no dependency list. The proxy's `@latest` and `.mod` endpoints supply the release
version and the requirement list, which is what the pinned-release and closure filters need.

THE PSEUDO-VERSION TRAP. Most of what the index streams is untagged: `v0.0.0-20240101000035-ebf15c`
is a synthesised stamp for a commit, not a release. Pinning a task to one pins it to a commit the
authors never blessed, so `filters.has_pinned_release` rejects them by pattern -- which is most of
this index's volume, and the reason its yield per page is low by construction rather than by fault.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from ..core.scale import Candidate
from . import filters
from .http import Http, NotFound, SourceError, all_or_nothing

INDEX = "https://index.golang.org/index?since=%s&limit=%d"
LATEST = "https://proxy.golang.org/%s/@latest"
MOD = "https://proxy.golang.org/%s/@v/%s.mod"

LANGUAGE = "go"

# The index caps a window at 2000 rows. Asking for more is silently truncated.
MAX_LIMIT = 2000

# Where a walk starts when the caller does not say. Recent enough that the modules are alive, old
# enough that there is real volume behind it.
DEFAULT_SINCE = "2024-01-01T00:00:00Z"


class GoModules:
    """The Go module index, paged by cursor, hydrated from the module proxy."""

    name = "pkg.go.dev"

    def __init__(self, http: Http | None = None, *, since: str = DEFAULT_SINCE,
                 scale: str = "package", hydrate: bool = True) -> None:
        self._http = http or Http()
        self._since = since
        self._scale = scale
        self._hydrate = hydrate
        # Page number -> the timestamp that page starts at. Page 0 is the caller's `since`; the rest
        # are learned by walking. Cached so that asking twice does not pay twice, which matters
        # because a cursor index cannot recompute an offset.
        self._cursors: dict = {0: since}

    def total(self) -> int | None:
        """None. The Go module index publishes no count, so there is nothing honest to return.

        Explicitly implemented rather than left off the class: `Index` requires the method to EXIST
        so that "how big is this" is always a question that can be asked, and None is the supported
        answer for an index that will not say. See core/sourcing.py.
        """
        return None

    def page(self, number: int, *, size: int = 20):
        limit = min(size, MAX_LIMIT)
        cursor = self._cursor_for(number, limit)
        if cursor is None:
            return []                       # walked off the end of the stream: genuinely exhausted
        rows = self._window(cursor, limit)
        if not rows:
            return []
        return all_or_nothing(rows, self._candidate, index=self.name)

    def _cursor_for(self, number: int, limit: int) -> str | None:
        """The timestamp page `number` starts at, walking forward if it is not known yet."""
        while number not in self._cursors:
            behind = max(k for k in self._cursors if k < number)
            rows = self._window(self._cursors[behind], limit)
            if not rows:
                return None
            self._cursors[behind + 1] = str(rows[-1].get("Timestamp") or "")
        return self._cursors[number]

    def _window(self, since: str, limit: int) -> list:
        """One window of the stream. Rows, parsed; a body it cannot read raises.

        A genuinely empty window IS how this index says exhausted -- the stream has an end -- so an
        empty answer is returned as it stands. What must not pass is a body that had lines and
        none of them parsed: an HTML maintenance page has plenty of lines and yields no rows, and
        forwarding that as an empty window would report the whole of pkg.go.dev as walked.
        """
        lines = self._http.lines(INDEX % (quote(since), limit))
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                # One malformed line in a stream is a row lost, not a window lost.
                continue
        if lines and not rows:
            raise SourceError(
                "%s returned %d line(s) and none of them were JSON, so the window cannot be read; "
                "returning it empty would report the index as exhausted"
                % (self.name, len(lines)))
        if not rows:
            return []
        # The next window starts at this one's last timestamp, and that row is INCLUSIVE -- verified
        # live. Recording the cursor here keeps the arithmetic in one place.
        return rows

    def _candidate(self, row: dict) -> Candidate:
        path = str(row.get("Path") or "")
        version = str(row.get("Version") or "")
        requirements = None
        release = version
        if self._hydrate:
            release, requirements = self._release(path, version)
        return to_candidate(path, release, scale=self._scale, source=self.name,
                            dependencies=requirements)

    def _release(self, path: str, fallback: str):
        """The module's latest release and how many modules it requires.

        A module with no releases at all 404s on `@latest`; that is one row lost, which
        `all_or_nothing` treats as a thinner page rather than an exhausted index.
        """
        escaped = _escape(path)
        try:
            latest = self._http.json(LATEST % escaped)
        except NotFound:
            return fallback, None
        version = str(latest.get("Version") or fallback)
        try:
            mod = self._http.get(MOD % (escaped, quote(version))).decode("utf-8", "replace")
        except NotFound:
            return version, None
        return version, _requirements(mod)


def to_candidate(path: str, version: str, *, scale: str = "package", source: str = "pkg.go.dev",
                 dependencies: int | None = None) -> Candidate:
    """A module path and version -> a Candidate."""
    facts = filters.Facts(
        name=path, version=version,
        # The index publishes no description. The path is all there is, and it carries real signal --
        # `.../internal/http` and `.../algorithms/sort` are both self-describing -- so the marker
        # tests run over it. `documented` is True because pkg.go.dev renders docs for every module,
        # which is what the contract-surface check is approximating.
        summary=path.replace("/", " ").replace("-", " "),
        keywords=tuple(path.split("/")),
        dependencies=dependencies, has_tests=None,
        repository=_repository(path), documented=True)
    return Candidate(
        identity="go:%s@%s" % (path, version),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": path, "version": version,
            "description": "the Go module %s at %s" % (path, version),
            "entry_points": [], "root": "",
            "install": ["go", "get", "%s@%s" % (path, version)],
            "forbidden": [path],
            "repository": facts.repository,
            "facts": filters.as_json(facts),
        })


def _escape(path: str) -> str:
    """The proxy's case-encoding: an upper-case letter becomes `!` + its lower-case form.

    Module paths are case-sensitive but the proxy is served from case-insensitive storage, so
    `github.com/BurntSushi/toml` is fetched as `github.com/!burnt!sushi/toml`. Getting this wrong
    produces a 404 for every module with a capital letter in its path -- which is a large minority
    of them, and it would look like those modules simply do not exist.
    """
    out = []
    for character in path:
        out.append("!" + character.lower() if character.isupper() else character)
    return quote("".join(out), safe="/.~_-!")


def _requirements(mod: str) -> int:
    """How many modules a go.mod requires, indirect ones excluded.

    Indirect requirements are the transitive closure Go writes down for reproducibility; counting
    them would measure the depth of the whole graph rather than what this module chose to depend on.
    """
    count, block = 0, False
    for line in mod.splitlines():
        line = line.strip()
        if line.startswith("require ("):
            block = True
            continue
        if block and line == ")":
            block = False
            continue
        if "// indirect" in line:
            continue
        if block and line and not line.startswith("//"):
            count += 1
        elif line.startswith("require ") and "(" not in line:
            count += 1
    return count


def _repository(path: str) -> str:
    """Where a module's source is, when the path says so.

    Go module paths are import paths, and for the hosts that serve most of them the first three
    segments ARE the repository. That is a convention rather than a rule -- a custom domain can
    serve a `go-import` meta tag pointing anywhere -- so this returns an empty string rather than
    guessing when the path is not one of the shapes it can read. An empty repository is a fact the
    filters already handle; a wrong one would send a clone somewhere that does not exist.
    """
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] in ("github.com", "gitlab.com", "bitbucket.org"):
        return "https://%s" % "/".join(parts[:3])
    return ""
