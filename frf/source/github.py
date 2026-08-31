"""GitHub, for the repo and module scales.

The other five indexes in this package are registries: they list published artefacts. This one lists
REPOSITORIES, which is what the repo scale needs and what the module scale needs a checkout of, and
it behaves differently enough to be worth its own file.

THE 1000-RESULT WALL, and it shapes everything here. GitHub's search returns at most 1000 results
per query no matter how many it says matched -- page 11 at 100 per page is an error, not an empty
page. A naive client walks 1000 repositories, stops, and reports the supply as exhausted when the
`total_count` it printed said 400,000. The fix is not to page harder: it is to SEGMENT the query so
that each segment holds fewer than a thousand, which is what `segments()` below does with star
ranges. That is the mechanical form of "an index must be enumerable" for a search that refuses to
enumerate itself.

WHY STAR RANGES AND NOT DATES. Both work, and stars have the property that matters more here: the
segment boundaries are stable. A repository's creation date never changes, but so does its star
count only slowly, and a run resumed a week later walks nearly the same segments -- whereas
`pushed:` moves under the query constantly and would make page 3 of yesterday and page 3 of today
different sets, which is exactly what the sourcing rule forbids.

RATE LIMITS ARE THE REAL CONSTRAINT. Search is 10 requests/minute unauthenticated and 30 with a
token, which is the difference between walking a segment in a minute and in three. `GITHUB_TOKEN` is
therefore read -- through `credentials`, never `os.environ` -- and its absence degrades rather than
failing, because a slow index still enumerates.
"""
from __future__ import annotations

import sys
import time
from urllib.parse import quote

from ..core.scale import Candidate
from . import filters
from .http import Http, all_or_nothing, envelope
from .tokens import TokenPool

SEARCH = "https://api.github.com/search/repositories?q=%s&sort=%s&order=asc&page=%d&per_page=%d"
COMMIT = "https://api.github.com/repos/%s/commits/%s"

# What one query can ever return, whatever `total_count` claims. Not a tuning knob: the API answers
# 422 past it. Everything about segmentation exists because of this number.
RESULT_CEILING = 1000
MAX_PER_PAGE = 100

# The star ranges a segmented walk uses, smallest last. Chosen so that each is under the ceiling for
# a typical language filter, and so that the most-starred repositories -- the ones most likely to be
# worth building a task from -- are walked first.
SEGMENTS = ((50000, None), (10000, 49999), (5000, 9999), (2000, 4999), (1000, 1999),
            (500, 999), (200, 499), (100, 199), (50, 99), (20, 49), (10, 19))

# `stars` rather than `best-match`. Relevance ranking is computed per request and is not promised to
# be stable; a star ordering with an explicit range is the closest this API comes to a fixed order.
DEFAULT_SORT = "stars"

# How many head-commit lookups run at once. One per repository and fifty to a page,
# so serial resolution is most of a page's wall time and all of it is latency. This is
# the core API (5000/hour) rather than search (30/minute), so the ceiling is comfort
# rather than quota -- but it stays small so one page cannot monopolise a token.
HEAD_WORKERS = 8


class GitHub:
    """Repository search, segmented so that more than a thousand results are reachable.

    One instance walks ONE query -- a language, plus whatever else the caller wants to constrain --
    across the star segments in order. The segment is part of what is walked, so it appears in the
    coverage log rather than being an invisible implementation detail.
    """

    name = "github"

    def __init__(self, http: Http | None = None, *, language: str = "",
                 query: str = "", scale: str = "repo", sort: str = DEFAULT_SORT,
                 segments: tuple = SEGMENTS, pin: bool = True,
                 token_pool: TokenPool | None = None) -> None:
        # Token pool for round-robin rotation and rate-limit awareness. Constructed here so that
        # each GitHub instance manages its own pool, but callers can share one by passing it in.
        self._pool = token_pool if token_pool is not None else TokenPool(
            warn=lambda m: print("WARNING: %s" % m, file=sys.stderr))
        token = self._pool.get_token() or ""
        # The token raises the limit from 10 to 30 requests a minute. Its absence is a slower walk,
        # never a failed one -- so it is applied if present and never required.
        self._http = http or Http(token=token)
        self._language = language
        self._query = query
        self._scale = scale
        self._sort = sort
        self._segments = tuple(segments)
        # Whether to resolve each repository's branch head to a commit. On by default, and the
        # default matters: search reports a BRANCH, a branch moves, and `has_pinned_release`
        # correctly refuses one -- so without this every repository is filtered out for having no
        # version we could write down. One extra request per repository buys an identity that means
        # the same thing next month, which is what the sourcing rule asks of an identity.
        self._pin = pin
        self._total: int | None = None
        # Where each page number begins, as (segment, offset). Recorded rather than computed --
        # see `page` for the failure the computation had.
        self._positions: dict = {0: (0, 0)}

    def total(self) -> int | None:
        """What GitHub says matched the unsegmented query.

        Honest but larger than what paging can reach, which is why `reachable()` exists beside it.
        Reporting only this would overstate the supply by orders of magnitude; reporting only the
        other would understate what is out there.
        """
        if self._total is None:
            self._fetch(self._query_for(None), 1, 1, counts_the_whole_index=True)
        return self._total

    def reachable(self) -> int:
        """The ceiling on what a segmented walk can actually get to."""
        return RESULT_CEILING * len(self._segments)

    def page(self, number: int, *, size: int = MAX_PER_PAGE):
        """One page, from whichever segment that page number falls in.

        The segment arithmetic is here rather than in the caller because `walk()` asks for page 0,
        1, 2 and must not have to know that page 10 is the last of segment one. A segment that runs
        out early is skipped over, so a caller sees one continuous sequence.

        WHY A RECORDED POSITION AND NOT `divmod`. The obvious arithmetic -- page N lives in segment
        `N // pages_per_segment` -- is wrong the moment a segment runs out early, and it fails in a
        way that looks like it is working. A short segment is retired, every later page number still
        maps into it, and each one is bumped forward to the NEXT segment at offset zero: page 3,
        page 4 and page 20 all return page 1 of segment 1 again, for ever. `walk()`'s deduplication
        hides the repetition from the output but not the cost -- a hundred candidates were measured
        costing fifty-six search requests instead of six, against an index that allows ten a minute
        -- and the material behind the repeated page is never reached at all, while the coverage
        record claims it was walked.

        So where each page ended is REMEMBERED. Sequential walking is then exact, re-asking for a
        page returns the same page, and the index still satisfies the rule it exists to serve:
        asking twice is a question with a stable answer.
        """
        size = min(max(1, size), MAX_PER_PAGE)
        index, offset = self._position_of(number, size)

        while index < len(self._segments):
            rows = self._fetch(self._query_for(self._segments[index]), offset + 1, size)
            if rows:
                self._positions[number + 1] = self._after(index, offset, len(rows), size)
                heads = self._heads_of(rows)
                return all_or_nothing(rows, lambda row: to_candidate(
                    row, scale=self._scale, commit=heads.get(id(row), "")), index=self.name)
            # An empty segment is not an exhausted index: there are more segments behind it. The
            # walk moves to the next one -- returning [] here would tell `walk()` that the whole of
            # GitHub had run out after one star range.
            index, offset = index + 1, 0

        self._positions[number + 1] = (index, 0)
        return []

    def _position_of(self, number: int, size: int) -> tuple:
        """Where page `number` starts. -> (segment index, offset within it).

        Recorded when the previous page was served. A caller that skips ahead to a page nobody has
        walked to gets the furthest position known, which is honest: this index cannot address an
        arbitrary page without walking to it, and pretending otherwise is what the arithmetic above
        was doing.
        """
        if number in self._positions:
            return self._positions[number]
        if not self._positions:
            return (0, 0)
        furthest = max(self._positions)
        return self._positions[furthest] if number >= furthest else (0, 0)

    def _after(self, index: int, offset: int, got: int, size: int) -> tuple:
        """Where the page after this one begins.

        A short page means the segment is spent -- GitHub gave everything it had -- so the next page
        starts the following segment. A full page advances within the segment until the thousandth
        result, which is the wall this whole class exists to work around.
        """
        if got < size or (offset + 1) * size >= RESULT_CEILING:
            return (index + 1, 0)
        return (index, offset + 1)

    def _heads_of(self, rows: list) -> dict:
        """Every row's head commit, resolved CONCURRENTLY. -> {id(row): sha}.

        One request per repository, and there are fifty rows to a page: resolved one after another
        that is most of a page's wall time, and it is pure latency rather than work. A stack dump of
        a live batch found the package and repo jobs both sitting here while module -- whose index
        widens rather than pages -- ran normally.
        (
        The requests are independent, so the fix is to stop waiting for each in turn. Bounded at a
        small number because this is the core API rather than search: generous enough to hide the
        latency, small enough not to spend an hour's quota on one page.
        )

        A row whose head cannot be resolved gets "", and `to_candidate` turns that into a candidate
        with no version -- which `filters.has_pinned_release` then refuses. That is the same outcome
        as before; the failure is not swallowed, it is deferred to the filter that already had an
        opinion about it.
        """
        if not self._pin:
            return {}
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=HEAD_WORKERS) as pool:
            futures = {id(row): pool.submit(self._head_of, row) for row in rows}
            resolved = {}
            for key, future in futures.items():
                try:
                    resolved[key] = future.result()
                except Exception:                              # noqa: BLE001 -- one row, not a page
                    resolved[key] = ""
        return resolved

    def _head_of(self, row: dict) -> str:
        """The commit a repository's default branch currently points at, or "".

        A 404 here is one repository that moved or went private between the search and this call,
        and `all_or_nothing` turns a page of those into an error rather than into a page that looks
        like exhaustion.
        """
        if not self._pin:
            return ""
        full_name = str(row.get("full_name") or "")
        branch = str(row.get("default_branch") or "")
        if not full_name or not branch:
            return ""
        payload = self._http.json(COMMIT % (full_name, quote(branch)),
                                  accept="application/vnd.github+json")
        return str(payload.get("sha") or "")

    def _query_for(self, segment) -> str:
        parts = [self._query] if self._query else []
        if self._language:
            parts.append("language:%s" % self._language)
        if segment is not None:
            low, high = segment
            parts.append("stars:>=%d" % low if high is None else "stars:%d..%d" % (low, high))
        # Repositories nobody has touched in years are usually unbuildable, and a mirror is somebody
        # else's repository counted twice. Both are cheap to exclude here and expensive to discover
        # after a clone.
        parts.append("archived:false")
        parts.append("mirror:false")
        return " ".join(parts) or "stars:>10"

    def _fetch(self, query: str, page_number: int, size: int, *,
               counts_the_whole_index: bool = False) -> list:
        """One search request. -> its rows.

        Before each request, rotate to the next available token so that rate limits on one
        token do not stall the walk. After the response, report the observed rate-limit headers
        back to the pool so exhausted tokens are skipped until their window resets.
        """
        # Rotate to a fresh token before each network call.
        token = self._pool.get_token() or ""
        if token != self._http.token:
            self._http.token = token

        payload = self._http.json(
            SEARCH % (quote(query), self._sort, page_number, size),
            accept="application/vnd.github+json")

        # Report rate-limit state from response headers so the pool can skip exhausted tokens.
        if token:
            hdrs = getattr(self._http, "last_headers", {})
            remaining_str = hdrs.get("x-ratelimit-remaining")
            reset_str = hdrs.get("x-ratelimit-reset")
            try:
                remaining = int(remaining_str) if remaining_str is not None else None
            except (TypeError, ValueError):
                remaining = None
            try:
                # X-RateLimit-Reset is a Unix epoch timestamp; convert to monotonic.
                reset_epoch = float(reset_str) if reset_str is not None else None
                reset_mono = (time.monotonic() + (reset_epoch - time.time())
                              if reset_epoch is not None else time.monotonic() + 60.0)
            except (TypeError, ValueError):
                reset_mono = time.monotonic() + 60.0
            self._pool.report_rate_limit(token, remaining, reset_mono)

        if counts_the_whole_index:
            self._total = int(payload.get("total_count") or 0)
        # `items` insisted upon rather than defaulted. An empty `items` is how a segment says it is
        # spent, and the loop above relies on that -- so a body with NO `items` at all must not be
        # allowed to look the same, or an error envelope would silently retire a star segment and
        # the walk would report material it never reached as walked.
        return envelope(payload, "items", index=self.name)


def to_candidate(row: dict, *, scale: str = "repo", source: str = "github",
                 commit: str = "") -> Candidate:
    """One repository -> a Candidate.

    The identity carries the COMMIT when one was resolved. That is what makes a repository identity
    mean the same thing in a later run: `github:owner/name` alone names a moving target, and a
    memory of it would suppress a repository whose contents have completely changed.
    """
    full_name = str(row.get("full_name") or "")
    facts = _facts(row, commit)
    return Candidate(
        identity="github:%s@%s" % (full_name, commit) if commit else "github:%s" % full_name,
        scale=scale,
        language=str(row.get("language") or "").lower(),
        source=source,
        detail={
            "repository": str(row.get("clone_url") or ""),
            "identity": full_name,
            "description": str(row.get("description") or ""),
            "default_branch": str(row.get("default_branch") or ""),
            "commit": commit,
            "stars": int(row.get("stargazers_count") or 0),
            "size_kb": int(row.get("size") or 0),
            "license": ((row.get("license") or {}) or {}).get("spdx_id") or "",
            # The repo scale needs these and search cannot supply them: how the project builds and
            # how it is invoked are read from the checkout. Present and empty so that a scale
            # reading `detail` finds the key rather than a KeyError.
            "build": [], "invoke": [],
            "facts": filters.as_json(facts),
        })


def _facts(row: dict, commit: str = "") -> filters.Facts:
    topics = tuple(str(t) for t in (row.get("topics") or ()))
    return filters.Facts(
        name=str(row.get("full_name") or ""),
        # THE COMMIT, not the branch. A branch name is not a version: it names wherever the project
        # has got to, and `has_pinned_release` refuses it -- correctly, since an expectation frozen
        # against "main" describes whatever main was that afternoon. With no commit resolved this is
        # empty and the candidate is filtered out, which is the honest outcome rather than a bug.
        version=commit,
        summary=str(row.get("description") or ""),
        keywords=topics,
        # Search reports neither. Unknown rather than absent, which the filters accept.
        dependencies=None, has_tests=None,
        repository=str(row.get("html_url") or ""),
        yanked=bool(row.get("archived")),
        documented=bool(row.get("has_wiki") or row.get("homepage")),
        extra={"stars": int(row.get("stargazers_count") or 0),
               "forks": int(row.get("forks_count") or 0),
               "open_issues": int(row.get("open_issues_count") or 0)})
