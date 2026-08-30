import io
import urllib.error

import pytest

from frf.core import model


def _gateway(monkeypatch):
    """Point `ask` at a configured gateway without touching the real credentials."""
    monkeypatch.setattr(model.credentials, "get", lambda name: {
        "LLM_BASE_URL": "https://example.invalid/v1", "LLM_API_KEY": "key",
        "LLM_MODEL": "test",
    }.get(name, ""))


def _http_error(code: int, body: str = "{}", headers=None):
    return urllib.error.HTTPError("https://example.invalid/v1", code, "no", headers,
                                  io.BytesIO(body.encode("utf-8")))


class _Answered:
    """A urlopen stand-in: raise the queued failures, then answer."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.attempts = 0

    def __call__(self, request, timeout):
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return _Reply('{"choices": [{"message": {"content": "ok"}}]}')


class _Reply:
    def __init__(self, body): self.body = body.encode("utf-8")
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *_): return False


def test_a_depleted_balance_is_waited_out_rather_than_failing_the_candidate(monkeypatch):
    """402/403 is the gateway's moment, not our request. It resets; the candidate should survive.

    Without this a balance dip mid-run kills whatever candidates were in flight, and a long batch
    spends its budget re-sourcing after our own outages instead of walking the supply.
    """
    _gateway(monkeypatch)
    urlopen = _Answered([_http_error(402, '{"code": "INSUFFICIENT_BALANCE"}'),
                         _http_error(403, '{"code": "INSUFFICIENT_BALANCE"}')])
    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)

    assert model.ask("hello", timeout=30) == "ok"
    assert urlopen.attempts == 3


def test_rate_limit_and_gateway_faults_are_retried(monkeypatch):
    """429 and the 5xx family describe the gateway, so the same body succeeds a moment later."""
    _gateway(monkeypatch)
    urlopen = _Answered([_http_error(429), _http_error(503), _http_error(500)])
    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)

    assert model.ask("hello", timeout=30) == "ok"
    assert urlopen.attempts == 4


def test_a_deterministic_refusal_is_not_retried(monkeypatch):
    """401 fails identically for ever. Retrying it burns the caller's timeout to learn nothing."""
    _gateway(monkeypatch)
    urlopen = _Answered([_http_error(401, '{"error": "bad key"}')] * 6)
    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)

    with pytest.raises(model.ModelError, match="answered 401"):
        model.ask("hello", timeout=30)
    assert urlopen.attempts == 1


def test_a_persistent_refusal_reports_what_the_gateway_said(monkeypatch):
    """Not "exceeded its timeout" -- that would hide the balance behind our own clock."""
    _gateway(monkeypatch)
    # A fresh error per attempt: `[x] * 20` would repeat ONE object whose body the first read
    # drains, so the assertion would be about the test's plumbing rather than the message.
    urlopen = _Answered([_http_error(402, '{"code": "INSUFFICIENT_BALANCE"}') for _ in range(20)])
    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)

    with pytest.raises(model.ModelError, match="answered 402.*INSUFFICIENT_BALANCE"):
        model.ask("hello", timeout=30)
    assert urlopen.attempts <= model.MAX_ATTEMPTS


def test_retrying_a_refusal_stays_inside_the_caller_s_budget(monkeypatch):
    """The timeout is a TOTAL budget. A backoff that would outlast it is not slept through."""
    _gateway(monkeypatch)
    urlopen = _Answered([_http_error(429)] * 20)
    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    slept = []
    monkeypatch.setattr(model.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(model.ModelError, match="answered 429"):
        model.ask("hello", timeout=0.1)
    assert sum(slept) <= 0.1


def test_the_gateway_s_own_retry_after_is_obeyed(monkeypatch):
    """A gateway that says when to come back knows better than our doubling schedule."""
    _gateway(monkeypatch)
    urlopen = _Answered([_http_error(429, "{}", {"Retry-After": "7"})])
    monkeypatch.setattr(model.urllib.request, "urlopen", urlopen)
    slept = []
    monkeypatch.setattr(model.time, "sleep", lambda s: slept.append(s))

    assert model.ask("hello", timeout=60) == "ok"
    assert slept == [7.0]


def test_model_timeout_is_total_across_transport_retries(monkeypatch):
    calls = []

    def unavailable(request, timeout):
        calls.append(timeout)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(model.credentials, "get", lambda name: {
        "LLM_BASE_URL": "https://example.invalid/v1", "LLM_API_KEY": "key",
        "LLM_MODEL": "test",
    }.get(name, ""))
    monkeypatch.setattr(model.urllib.request, "urlopen", unavailable)
    with pytest.raises(model.ModelError, match="did not answer"):
        model.ask("hello", timeout=0.1)
    assert len(calls) <= 3
    assert max(calls) <= 0.11
