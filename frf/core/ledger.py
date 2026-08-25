"""Append-only batch ledger for auditable production runs."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class LedgerRecord:
    identity: str
    scale: str
    status: str
    stage: str = ""
    reason: str = ""
    fault: str = ""
    path: str = ""
    seconds: float = 0.0
    timestamp: str = ""


class BatchLedger:
    """Crash-safe JSONL ledger; one record per candidate attempt."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def append(self, record: LedgerRecord) -> None:
        value = asdict(record)
        value["timestamp"] = value["timestamp"] or datetime.now(timezone.utc).isoformat()
        fd, tmp = tempfile.mkstemp(prefix=".ledger-", dir=os.path.dirname(os.path.abspath(self.path)))
        try:
            existing = open(self.path, "rb").read() if os.path.exists(self.path) else b""
            with os.fdopen(fd, "wb") as handle:
                handle.write(existing)
                handle.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise
