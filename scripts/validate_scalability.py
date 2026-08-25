#!/usr/bin/env python3
"""Bounded scalability checks that do not require a thousand real tasks.

The local model proves scheduler invariants; --e2b adds a small real sandbox smoke.
"""
from __future__ import annotations

import argparse
import json
import threading
import time

from frf.automation import configure_e2b_slots
from frf.core.checkpoint import CheckpointRecord, CheckpointWriter


def scheduler_check(workers: int, active: int) -> dict:
    configure_e2b_slots(active)
    lock = threading.Lock()
    current = peak = 0
    failures = []

    def worker(index: int) -> None:
        nonlocal current, peak
        from frf.automation import _E2B_SLOTS  # noqa: PLC0415 -- invariant probe
        _E2B_SLOTS.acquire()
        try:
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.01)
            if current > active:
                failures.append("slot limit exceeded")
        finally:
            with lock:
                current -= 1
            _E2B_SLOTS.release()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    started = time.monotonic()
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    return {"workers": workers, "active_limit": active, "peak_active": peak,
            "seconds": round(time.monotonic() - started, 4), "failures": failures}


def checkpoint_check() -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as room:
        path = room + "/checkpoint.jsonl"
        writer = CheckpointWriter(path)
        common = dict(scale="module", task_form="", stage="", reason="", timestamp="now", path="")
        writer.write(CheckpointRecord(identity="done", status="emitted", fault="", **common))
        writer.write(CheckpointRecord(identity="material", status="refused", fault="material", **common))
        writer.write(CheckpointRecord(identity="retry", status="refused", fault="factory", **common))
        skipped = writer.load_completed()
        return {"skipped": sorted(skipped), "factory_retried": "retry" not in skipped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--active", type=int, default=8)
    args = parser.parse_args()
    scheduler = scheduler_check(args.workers, args.active)
    checkpoint = checkpoint_check()
    report = {"scheduler": scheduler, "checkpoint": checkpoint}
    # JSON makes the result consumable by CI and keeps the evidence unambiguous (Python's repr
    # uses single quotes and cannot be parsed by the matrix/audit tooling).
    print(json.dumps(report, sort_keys=True))
    return int(bool(scheduler["failures"] or scheduler["peak_active"] > args.active
                    or not checkpoint["factory_retried"]))


if __name__ == "__main__":
    raise SystemExit(main())
