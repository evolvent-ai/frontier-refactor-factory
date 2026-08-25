"""Offline contracts for the open-world language matrix report."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from frf.core.capabilities import capability


def _module():
    path = Path(__file__).parents[1] / "scripts" / "validate_language_matrix.py"
    spec = importlib.util.spec_from_file_location("validate_language_matrix", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_registered_adapter_without_scale_is_not_reported_as_discovered():
    item = capability("javascript", scale="module")
    assert item.level == "call-capable"
    assert item.adapter == "javascript"


def test_matrix_row_preserves_unknown_language_and_audit_placeholders(monkeypatch):
    matrix = _module()

    class Index:
        rejection_counts = {"adapter-not-registered": 1}

        def page(self, _number, *, size):
            assert size == 1
            return []

    monkeypatch.setattr(matrix, "_index", lambda *args, **kwargs: Index())
    row = matrix.collect(["zig"], "repo", 1)[0]

    assert row["capability"]["level"] == "discovered"
    assert row["adapter_status"] == "unregistered"
    assert row["source_eligible"] is False
    assert row["e2b_smoke"] == "not-run"
    assert row["harbor"] == "not-run"
    assert row["yield"] is None
    assert row["concurrency"]["peak_active"] is None


def test_matrix_source_errors_are_recorded_without_certifying_the_row(monkeypatch):
    matrix = _module()

    def fail(*_args, **_kwargs):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(matrix, "_index", fail)
    row = matrix.collect(["python"], "package", 1)[0]
    assert row["source_eligible"] is False
    assert row["errors"] == ["network unavailable"]
    assert row["replay"] == "not-run"


def test_matrix_reports_capability_level_as_adapter_status(monkeypatch):
    matrix = _module()

    class Index:
        rejection_counts = {}

        def page(self, _number, *, size):
            return [SimpleNamespace(identity="x", language="javascript", capability={})]

    # Import locally to keep the test's fixture declaration clear and avoid coupling Candidate's
    # full validation to this report-format test.
    from types import SimpleNamespace
    monkeypatch.setattr(matrix, "_index", lambda *args, **kwargs: Index())
    row = matrix.collect(["javascript"], "package", 1)[0]
    assert row["adapter_status"] == "call-capable"


def test_batch_report_merge_only_promotes_explicit_evidence():
    matrix = _module()
    row = {"language": "python", "scale": "package", **matrix._evidence_fields()}
    merged = matrix.apply_batch_report(row, {
        "summary": {"emitted": 1, "attempted": 2, "yield_rate": 0.5,
                    "metrics": {"batch_seconds": 3.25}},
        "seconds": 3.5,
    })
    assert merged["yield"] == 0.5
    assert merged["seconds"] == 3.5
    assert merged["replay"] == "not-run", "emitted is not proof of replay"


def test_collect_matrix_emits_every_language_scale_pair(monkeypatch):
    matrix = _module()

    class Index:
        rejection_counts = {}

        def page(self, _number, *, size):
            return []

    monkeypatch.setattr(matrix, "_index", lambda *args, **kwargs: Index())
    rows = matrix.collect_matrix(["zig"], 1, scales=("module", "repo"))
    assert [(row["language"], row["scale"]) for row in rows] == [
        ("zig", "module"), ("zig", "repo")]
