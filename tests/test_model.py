import urllib.error

import pytest

from frf.core import model


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
