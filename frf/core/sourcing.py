"""Where candidates come from, and why it may only be somewhere countable.

    A CANDIDATE LIST MUST COME FROM AN ENUMERABLE INDEX.
    A model may rank and filter candidates. It may not produce them.

That rule is the whole of what makes scalability answerable, and it is worth being precise about why
the obvious alternative fails. Asking a model to name packages worth optimising works -- it returns
plausible names, and some become tasks. What it cannot do is answer "how much material is left":
there is no page to advance, no total to subtract from, and no way to know whether the names it just
gave you are the same ones it gave you last week. The supply looks infinite until it silently is not,
and a yield computed against it means nothing because the denominator was never real.

An index has all three. It can be paged, its results can be counted, and asking it twice is a
question with a stable answer. So the factory sources from indexes -- code search, package
registries, reverse-dependency graphs -- and a model, if it is used at all, only ever reorders a
list that already exists.

WHAT AN INDEX IS, here: anything that can answer `page(n)` and does not change its mind between
pages. Nothing in this module knows about HTTP or about any particular registry; a scale supplies an
`Index` and this supplies the memory, the deduplication and the accounting that go around it.

WHY MEMORY IS NOT OPTIONAL. Without it a run rediscovers what the last run already refused, and the
yield falls as a batch grows for a reason that has nothing to do with the material. The seen-set is
therefore part of sourcing rather than an optimisation on top of it.

RESILIENCE. Network errors and validation failures are retried with exponential backoff (up to 3
retries). A candidate that fails validation is skipped rather than crashing the walk. When a source
is exhausted or errors repeatedly, the event is logged but the walk continues -- one bad index does
not kill a batch.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Protocol

from .scale import Candidate


class Index(Protocol):
    """Something that can be paged through and does not reorder itself between pages.

    The second half is the requirement that matters. An index that returns results in a different
    order each call cannot be resumed: page 3 of this run and page 3 of the next are different sets,
    so "we have walked 300 of 12,000" stops being a fact.
    """

    name: str

    def page(self, number: int, *, size: int) -> Iterable[Candidate]: ...

    def total(self) -> int | None:
        """How many candidates exist, or None when the index will not say.

        None is honest and usable -- it means "unknown", and the accounting below reports coverage
        as unknown rather than inventing a denominator. What is NOT acceptable is a source that
        cannot even be asked, which is why this method exists at all.
        """


@dataclass
class Memory:
    """What has already been tried, so that a later run does not rediscover it.

    Persisted as one JSON file rather than a database: it is a set of strings that a person should
    be able to read, edit and delete. The identity is the key, so it has to be stable across runs --
    a URL and a pinned ref, never a position in a result list.
    """

    path: str = ""
    seen: set = field(default_factory=set)

    @classmethod
    def load(cls, path: str) -> "Memory":
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return cls(path, set(json.load(handle).get("seen", ())))
        return cls(path, set())

    def remember(self, candidate: Candidate) -> None:
        self.seen.add(candidate.identity)

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"seen": sorted(self.seen)}, handle, indent=1)

    def __contains__(self, candidate: Candidate) -> bool:
        return candidate.identity in self.seen

    def __len__(self) -> int:
        return len(self.seen)


@dataclass
class Coverage:
    """How much of an index has been walked. The number that makes a yield mean something.

    A yield of 3% is a different fact when 200 of 12,000 candidates have been seen than when 11,800
    have. The first says the material is plentiful and the filter is strict; the second says the
    supply is nearly gone. Reporting the yield without this is how a factory discovers it has run
    out by producing nothing for a week.
    """

    index: str
    walked: int = 0
    fresh: int = 0
    repeats: int = 0
    total: int | None = None

    @property
    def remaining(self) -> int | None:
        return None if self.total is None else max(0, self.total - self.walked)

    def to_json(self) -> dict:
        return {"index": self.index, "walked": self.walked, "fresh": self.fresh,
                "repeats": self.repeats, "total": self.total, "remaining": self.remaining}


def batch_memory(owner) -> Memory:
    """The seen-set one scale reuses across successive `find()` calls. Created on first use.

    WHY SUCCESSIVE CALLS NEED TO SHARE ONE. `walk` restarts its paging at zero on every call, and an
    index maps page 0 to the same first page for ever -- correctly, since "asking twice is a
    question with a stable answer" is the property the whole module is built on. With a fresh
    `Memory` per call, a roll that asks for four candidates, tries one, and asks again gets THE SAME
    FOUR back. The caller's own seen-set then filters all of them out, and the loop concludes the
    index is spent.

    That is not hypothetical. A roll configured for fifteen attempts made ONE: waves are capped at
    the remaining target, which is 1 when the budget is 1, so each round consumed a single candidate
    and the next round rediscovered it. Three cells reported `attempted: 1` against
    `max_attempts: 15` and were read as thin material.

    Sharing the memory makes `walk` page FORWARD past what it has already handed out, so the second
    call returns candidates the first did not. The scale owns it rather than the batch loop because
    `find(budget)` takes no memory argument, and threading one through three scales' signatures
    would put the fix in the callers rather than in the thing that has the state.

    PERSISTED WHEN `FRF_SOURCING_MEMORY` NAMES A FILE, and that turned out to matter far more than
    it looks. A batch restarted four times -- to pick up fixes -- re-walked from page one each time
    and re-emitted the same top candidates: 55 emissions collapsed to 20 distinct subjects, so about
    two thirds of the sandbox time went on material already produced. `Memory` was built for exactly
    this and nothing had ever handed it a path.

    Unset, it stays per-instance and dies with the batch, which is the right default for a one-shot
    run. Set, a restarted or resumed roll continues through the supply instead of re-mining its
    head -- and the seen-set is a plain sorted JSON list somebody can read and prune.
    """
    existing = getattr(owner, "_batch_memory", None)
    if existing is None:
        path = (os.environ.get("FRF_SOURCING_MEMORY") or "").strip()
        existing = Memory.load(path) if path else Memory()
        try:
            owner._batch_memory = existing
        except Exception:                                  # noqa: BLE001 -- slotted/frozen owner
            pass
    return existing


def walk(index: Index, budget: int, *, memory: Memory | None = None, page_size: int = 50,
         keep: Callable[[Candidate], bool] | None = None,
         language_filter: str | None = None,
         max_seconds: float | None = None,
         log: Callable[[str], None] = lambda _m: None) -> Iterator[Candidate]:
    """Yield up to `budget` candidates never seen before, and record what it cost to find them.

    A generator, because the caller builds a task from each candidate before asking for the next and
    a task takes minutes. Materialising the whole list first would mean paging an index for material
    that a batch stopped needing an hour ago.

    `keep` is the mechanical filter -- has tests, has a pinned release, is small enough to build.
    Cheap, deterministic, and applied before anything is cloned. What it must NOT be is a judgement
    about quality; that is what the pipeline's gates are for, and they answer with evidence.

    `language_filter` restricts the walk to candidates whose `language` attribute matches the given
    string (case-insensitive). Applied after memory deduplication and before `keep`, so that a
    language-filtered walk does not rediscover candidates from another language's prior run.

    RESILIENCE. Network errors during `index.page()` are retried with exponential backoff (up to 3
    retries, 1s/2s/4s delays). Candidates that fail validation (missing required fields) are skipped
    rather than crashing the entire walk. When an index is exhausted or errors repeatedly, the event
    is logged but sourcing continues.
    """
    memory = memory if memory is not None else Memory()
    if max_seconds is None:
        configured = os.environ.get("FRF_SOURCING_MAX_SECONDS", "").strip()
        try:
            max_seconds = float(configured) if configured else None
        except ValueError:
            max_seconds = None
    deadline = None if max_seconds is None else time.monotonic() + max(0.0, max_seconds)
    # `total()` may itself be a remote request (GitHub search is the important example).  When a
    # caller supplies a wall-clock budget, do not spend that budget before the first page can be
    # inspected; an unknown denominator is honest and the walk can still report walked/fresh.
    indexed_total = None if max_seconds is not None else _total_of(index)
    coverage = Coverage(index.name, total=indexed_total)
    # Keep the machine-readable accounting on the index as well as in the human log.  Widening
    # adapters (functions, packages, repositories) can then report how much source they consumed
    # without changing the generator API or pretending that `budget` equals work performed.
    try:
        index.last_coverage = coverage
    except Exception:
        pass
    produced = 0
    page = 0
    consecutive_errors = 0
    max_consecutive_errors = 3

    while produced < budget:
        if deadline is not None and time.monotonic() >= deadline:
            log("%s: sourcing deadline reached after %d candidate(s)" % (index.name, coverage.walked))
            break
        batch = _fetch_page_with_retry(index, page, page_size, log)
        if batch is None:
            # Retry limit exhausted; skip this page.
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                log("%s: too many consecutive errors (%d), stopping walk"
                    % (index.name, consecutive_errors))
                break
            page += 1
            continue
        if not batch:
            log("%s: exhausted after %d page(s)" % (index.name, page))
            break
        consecutive_errors = 0
        page += 1
        log("%s: page %d returned %d row(s)" % (index.name, page - 1, len(batch)))
        for candidate in batch:
            # Validate candidate has required fields; skip malformed ones.
            if not _is_valid_candidate(candidate):
                log("%s: skipping invalid candidate: %s" % (index.name, candidate))
                continue
            coverage.walked += 1
            if candidate in memory:
                coverage.repeats += 1
                continue
            memory.remember(candidate)
            # Apply language filter if requested.
            if language_filter:
                lang_lower = (candidate.language or "").lower()
                filter_lower = language_filter.strip().lower()
                if lang_lower != filter_lower:
                    continue
            if keep is not None and not keep(candidate):
                log("%s: rejected candidate %s by mechanical filter" %
                    (index.name, candidate.identity))
                continue
            coverage.fresh += 1
            produced += 1
            yield candidate
            if produced >= budget:
                break

    memory.save()
    try:
        index.last_coverage = coverage
    except Exception:
        pass
    log("%s: %s" % (index.name, json.dumps(coverage.to_json())))


def _total_of(index: Index) -> int | None:
    """An index that cannot say how big it is reports None rather than failing the run.

    Some genuinely do not know -- a code search caps its own result count -- and refusing to source
    from them would be trading a usable supply for a tidy number.
    """
    try:
        return index.total()
    except Exception:                                          # noqa: BLE001 -- unknown, not fatal
        return None


def _fetch_page_with_retry(index: Index, page_number: int, size: int,
                            log: Callable[[str], None]) -> list | None:
    """Fetch one page with exponential backoff on retryable failures. -> None when retries exhausted."""
    from ..source.http import SourceError, TransportError  # avoid circular import

    for attempt in range(3):
        try:
            return list(index.page(page_number, size=size))
        except TransportError as error:
            delay = (2 ** attempt) * 1.0
            log("%s: page %d failed (attempt %d/3): %s; retrying in %.1fs"
                % (index.name, page_number, attempt + 1, error, delay))
            time.sleep(delay)
        except SourceError as error:
            # Non-transport errors (e.g. parsing failures) are not retryable.
            log("%s: page %d failed permanently: %s" % (index.name, page_number, error))
            return None
        except Exception as error:   # noqa: BLE001
            log("%s: page %d unexpected error: %s" % (index.name, page_number, error))
            return None
    return None


def _is_valid_candidate(candidate: Candidate) -> bool:
    """Whether a candidate has the required fields to be usable downstream."""
    if not candidate.identity or not candidate.scale or not candidate.source:
        return False
    return True
