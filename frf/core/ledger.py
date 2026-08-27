"""Append-only batch ledger for auditable production runs."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


def make_ledger_path(prefix: str = "frf-ledger") -> str:
    """Auto-name a ledger from the clock, as checkpoints are named.

    WHY A DEFAULT EXISTS AT ALL. `ledger_file` defaulted to the empty string and the writer was
    guarded by `if ledger_file:`, so a batch run without that one setting recorded nothing about what
    it attempted or why it refused what it refused. Every batch this factory has run took that
    branch: there is not a single ledger on disk. A run that produces no record of its own decisions
    cannot be audited afterwards, and the handoff notes asking for per-language yield evidence were
    asking for exactly the file nobody was writing.
    """
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "%s-%s.jsonl" % (prefix, stamp)


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
