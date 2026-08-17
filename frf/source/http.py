"""The one way this package reaches the network, and the failures it refuses to hide.

STANDARD LIBRARY ONLY. `urllib` rather than `requests`, so that sourcing works in an installation
with no extras at all. The `sourcing` extra in pyproject.toml exists for other reasons and nothing
in this package imports it; if that ever changes, the import must be lazy and the degradation must
say what is missing, because a factory that cannot source is a factory with no supply and the
message it prints is the whole of the user's diagnosis.

THE DISTINCTION THIS MODULE EXISTS TO PROTECT. `walk()` in core/sourcing.py stops when a page comes
back empty, because an empty page is how an index says "exhausted". So a client that catches a
timeout and returns `[]` does not report a network problem -- it reports that the registry has no
more packages, and the batch ends quietly, three hundred candidates short, with a coverage record
that says the supply ran out. That failure is invisible: nothing raised, nothing logged, the numbers
merely became wrong. Therefore EVERY failure here raises, and the only thing that may produce an
empty page is a registry that genuinely returned no rows.

BEING POLITE IS SELF-INTEREST. A registry that blocks this factory removes an entire language's
supply, and there is no local fix for that. So: a descriptive User-Agent, a delay between requests,
and 429 honoured with the server's own Retry-After rather than a retry loop of our choosing.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Descriptive on purpose. A registry operator looking at their logs should be able to tell what this
# is and that it is not a scraper, without having to guess from a request pattern.
USER_AGENT = ("frontier-refactor-factory/0.1 (benchmark task sourcing; "
              "low-rate metadata reads; https://pypi.org/help/#user-agent)")

# Between requests, seconds. Small enough that a page of fifty is not a coffee break, large enough
# that a batch does not look like a burst to a rate limiter.
POLITE_DELAY = 0.2

# How many times a retryable failure is retried before it becomes the caller's problem. Retrying
# forever would turn a registry outage into a hung run, which is harder to diagnose than a failure.
ATTEMPTS = 4

# Longest a backoff will wait, seconds. A registry asking for more than this via Retry-After is
# telling us to come back later, and the honest response is to fail the run rather than sleep
# through it holding a sandbox open.
MAX_BACKOFF = 60.0


class SourceError(RuntimeError):
    """Something went wrong reaching an index. ALWAYS raised, never swallowed into an empty page.

    The base of the hierarchy so that a caller who does not care why can still write one `except`
    around a batch and know it did not silently source nothing.
    """


class TransportError(SourceError):
    """The request did not complete: DNS, TCP, TLS, timeout, or a 5xx after retries.

    Distinct from `HttpError` because the two lead different places -- this one means try again
    later or check the network, and that one means the request itself was wrong.
    """


class HttpError(SourceError):
    """The server answered, and the answer was not success."""

    def __init__(self, message: str, *, status: int = 0, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class NotFound(HttpError):
    """404. The one HTTP failure a client is allowed to treat as "skip this item".

    A registry's name list and its per-package metadata are not updated atomically, so a name that
    was in the index a second ago can 404 now. That is one dead item, not a dead page -- and the
    difference is enforced by `all_or_nothing` below rather than left to each client's judgement.
    """


class Unauthorized(HttpError):
    """401/403 where the endpoint requires a credential this process does not have.

    Names the credential in the message. GitHub's code search is the live example: it refuses
    unauthenticated callers outright, so the absence of GITHUB_TOKEN cannot be papered over -- but
    it must fail HERE, at the call, rather than at construction, so that a factory holding a code
    index it never pages still starts.
    """


class RateLimited(HttpError):
    """429, or a 403 that a rate limiter dressed up. Carries what the server asked us to wait."""

    def __init__(self, message: str, *, status: int = 429, url: str = "",
                 retry_after: float | None = None) -> None:
        super().__init__(message, status=status, url=url)
        self.retry_after = retry_after


@dataclass
class Http:
    """A small, polite, retrying GET. Injected into every client so tests can replace it.

    Injection rather than module-level functions because the offline half of the test suite has to
    exercise the PARSING -- the part that actually breaks when a registry changes a field name --
    and it cannot do that if the transport is reached through an import.
    """

    user_agent: str = USER_AGENT
    delay: float = POLITE_DELAY
    timeout: float = 30.0
    attempts: int = ATTEMPTS
    token: str = ""                       # bearer credential, when an index has one
    _last_call: float = field(default=0.0, init=False, repr=False)
    calls: int = field(default=0, init=False, repr=False)

    def get(self, url: str, *, accept: str = "", headers: dict | None = None) -> bytes:
        """The body, or an exception. There is no third outcome, and that is the point."""
        request_headers = {"User-Agent": self.user_agent}
        if accept:
            request_headers["Accept"] = accept
        if self.token:
            request_headers["Authorization"] = "Bearer %s" % self.token
        request_headers.update(headers or {})

        last: Exception | None = None
        for attempt in range(max(1, self.attempts)):
            self._wait()
            try:
                self.calls += 1
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                last = _translate(error, url)
                # Only two statuses are worth trying again. A 404 or a 400 will be a 404 or a 400
                # the second time, and retrying them spends the politeness budget on nothing.
                if not isinstance(last, (RateLimited, TransportError)):
                    raise last
                _sleep(_backoff(attempt, getattr(last, "retry_after", None)))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = TransportError("%s did not answer: %s" % (url, error))
                _sleep(_backoff(attempt, None))
        raise last if last is not None else TransportError("%s: no attempt was made" % url)

    def json(self, url: str, *, accept: str = "application/json",
             headers: dict | None = None):
        """A parsed body. A body that is not JSON is a transport-level surprise, not a value.

        Registries have been known to answer a 200 with an HTML maintenance page. Letting that
        reach a parser produces a KeyError three frames away from the cause, so it is named here.
        """
        body = self.get(url, accept=accept, headers=headers)
        try:
            return json.loads(body)
        except ValueError as error:
            raise TransportError("%s answered with something that is not JSON (%d bytes): %s"
                                 % (url, len(body), error)) from error

    def lines(self, url: str, *, accept: str = "", headers: dict | None = None) -> list[str]:
        """Non-empty lines of a text body. For the two indexes that answer in newline-delimited
        formats rather than in a JSON envelope."""
        return [line for line in self.get(url, accept=accept, headers=headers)
                .decode("utf-8", "replace").splitlines() if line.strip()]

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_call = time.monotonic()


def _translate(error: urllib.error.HTTPError, url: str) -> SourceError:
    """One HTTP status -> the exception a caller can act on.

    A table rather than a chain of ifs at each call site, because "is a 403 a rate limit or a
    permission problem" is a question every client would otherwise answer slightly differently --
    and GitHub, which is the reason the question is hard, is not the only registry that reuses it.
    """
    detail = ""
    try:
        detail = error.read().decode("utf-8", "replace")[:300]
    except Exception:                                       # noqa: BLE001 -- the body is a bonus
        pass
    headers = getattr(error, "headers", None)
    remaining = (headers or {}).get("X-RateLimit-Remaining") if headers else None
    retry_after = _retry_after(headers)

    if error.code == 429 or (error.code == 403 and remaining == "0"):
        return RateLimited("%s is rate limiting this client%s" % (
            url, "" if retry_after is None else " (retry after %.0fs)" % retry_after),
            status=error.code, url=url, retry_after=retry_after)
    if error.code in (401, 403):
        return Unauthorized(
            "%s refused this request (%d). If this index needs a credential, set GITHUB_TOKEN in "
            "the environment or in .env -- see frf/core/credentials.py. %s"
            % (url, error.code, detail), status=error.code, url=url)
    if error.code == 404:
        return NotFound("%s: not found" % url, status=404, url=url)
    if error.code >= 500:
        # Retryable, so it wears the transport type. A registry having a bad minute is not a
        # statement about the request we sent.
        return TransportError("%s answered %d: %s" % (url, error.code, detail))
    return HttpError("%s answered %d: %s" % (url, error.code, detail), status=error.code, url=url)


def _retry_after(headers) -> float | None:
    """What the server asked for, in seconds, when it said so in a form we understand."""
    if not headers:
        return None
    raw = headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None                        # the HTTP-date form; the default backoff covers it


def _backoff(attempt: int, retry_after: float | None) -> float:
    """The server's number if it gave one, ours otherwise, capped either way."""
    if retry_after is not None and retry_after > 0:
        return min(retry_after, MAX_BACKOFF)
    return min(MAX_BACKOFF, POLITE_DELAY * (2 ** attempt) * 5)


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def all_or_nothing(rows: list, hydrate, *, index: str) -> list:
    """Per-item metadata lookups, where a page that loses EVERY row is an error.

    The subtle half of this module's contract. Hydration 404s happen -- a name is listed a moment
    before its metadata exists, or just after it was deleted -- and dropping that one row is right.
    Dropping all fifty is indistinguishable, to `walk()`, from a registry that ran out of packages,
    and it would end the batch and write a coverage record saying the supply is gone.

    So: some rows lost is a thinner page, and every row lost is raised. The asymmetry is the whole
    reason this is a function instead of a comprehension in each client.
    """
    kept, lost = [], []
    for row in rows:
        try:
            kept.append(hydrate(row))
        except NotFound as error:
            lost.append(str(error))
    if rows and not kept:
        raise SourceError(
            "%s: all %d row(s) on this page failed to resolve, so the page is empty for a reason "
            "that is not exhaustion. Returning it would tell walk() the index ran out. First: %s"
            % (index, len(rows), lost[0] if lost else "unknown"))
    return kept
