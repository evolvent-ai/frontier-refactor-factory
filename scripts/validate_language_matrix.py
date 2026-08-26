#!/usr/bin/env python3
"""Collect bounded open-world language/source eligibility evidence.

This is deliberately a source/smoke matrix, not a claim that every language is certified. Full
freeze/verifier/Harbor runs are expensive and are launched separately for eligible rows.
"""
from __future__ import annotations

import argparse
import json
import signal
from contextlib import contextmanager

from frf.automation import _index
from frf.core.capabilities import capability
from frf.core.scale import SCALES


_SOURCE_INDEX = {
    "repo": "github",
    "module": "github-functions",
    "kernel": "github-functions",
    "package": "github-packages",
}


def _evidence_fields() -> dict:
    """Stable fields for one language/scale audit row.

    Source collection is intentionally separate from execution.  A row can therefore describe a
    discovered or rejected language without pretending that a local probe was an E2B certification.
    The fields are also the contract consumed by production matrix reports.
    """
    return {
        "source_eligible": False,
        "adapter_status": "unregistered",
        "e2b_smoke": "not-run",
        "build": "not-run",
        "workload": "not-run",
        "freeze": "not-run",
        "replay": "not-run",
        "adequacy": "not-run",
        "verifier": "not-run",
        "harbor": "not-run",
        "yield": None,
        "seconds": None,
        "concurrency": {"workers": None, "active_limit": None, "peak_active": None},
    }


@contextmanager
def _row_deadline(seconds: float):
    """Bound one registry row without turning a slow endpoint into a hung matrix."""
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        yield
        return
    try:
        previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("matrix row timeout")))
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    except ValueError:  # called outside the main thread
        yield
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        except (ValueError, UnboundLocalError):
            pass


def collect(languages: list[str], scale: str, count: int, *, timeout: float = 60.0) -> list[dict]:
    if scale not in SCALES:
        raise ValueError("unknown scale %r; expected one of %s" % (scale, ", ".join(SCALES)))
    rows = []
    for language in languages:
        cap = capability(language, scale=scale)
        item = {"language": language, "scale": scale,
                "capability": cap.__dict__, "candidates": [], "errors": [],
                **_evidence_fields()}
        # Keep the adapter state aligned with the capability ladder so a matrix consumer can
        # distinguish a discovered language from a registered repo-only adapter and a certified
        # call adapter without reading two unrelated fields.
        item["adapter_status"] = cap.level if cap.adapter else "unregistered"
        try:
            with _row_deadline(timeout):
                index_name = _SOURCE_INDEX[scale]
                index = _index(index_name, subset=language, scale=scale)
                for candidate in list(index.page(0, size=count))[:count]:
                    item["candidates"].append({"identity": candidate.identity,
                                               "language": candidate.language,
                                               "capability": candidate.capability})
                item["source_rejections"] = dict(getattr(index, "rejection_counts", {}))
                item["source_eligible"] = bool(item["candidates"])
        except TimeoutError:
            item["errors"].append("matrix row timeout after %.1fs" % timeout)
            item["matrix_status"] = "timeout"
        except Exception as exc:
            item["errors"].append(str(exc)[:1000])
        rows.append(item)
    return rows


def apply_batch_report(row: dict, report: dict) -> dict:
    """Merge an executed batch report into a source row without inventing stage evidence.

    ``collect`` is intentionally cheap and source-only.  Production callers can run the selected
    candidate through ``automation.run`` and feed its JSON report here; only fields actually
    present in that report are promoted to evidence.  Missing stages stay ``not-run`` rather than
    being inferred from a non-zero emitted count.
    """
    merged = dict(row)
    summary = report.get("summary", report)
    metrics = summary.get("metrics", {})
    if "seconds" in report:
        merged["seconds"] = report["seconds"]
    elif "batch_seconds" in metrics:
        merged["seconds"] = metrics["batch_seconds"]
    if "yield_rate" in summary:
        merged["yield"] = summary["yield_rate"]
    if "harbor_checked" in summary:
        merged["harbor"] = ("passed" if summary.get("harbor_failed", 0) == 0 else "failed")
    for field in ("e2b_smoke", "build", "workload", "freeze", "replay", "adequacy", "verifier"):
        value = summary.get(field)
        if value is not None:
            merged[field] = value
    concurrency = summary.get("concurrency")
    if isinstance(concurrency, dict):
        merged["concurrency"] = {**merged["concurrency"], **concurrency}
    return merged


def collect_matrix(languages: list[str], count: int, scales=SCALES, *, timeout: float = 60.0) -> list[dict]:
    """Collect one auditable row for every requested language/scale pair."""
    rows = []
    for scale in scales:
        rows.extend(collect(languages, scale, count, timeout=timeout))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=(*SCALES, "all"), default="all")
    parser.add_argument("--languages", default="python,javascript,typescript,go,rust,java,ruby,cpp")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="maximum seconds per language/scale row (default: 60)")
    args = parser.parse_args()
    languages = [x.strip() for x in args.languages.split(",") if x.strip()]
    rows = (collect_matrix(languages, max(1, args.count), timeout=max(0, args.timeout)) if args.scale == "all"
            else collect(languages, args.scale, max(1, args.count), timeout=max(0, args.timeout)))
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
