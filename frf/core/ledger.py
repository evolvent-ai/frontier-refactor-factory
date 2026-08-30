"""Append-only batch ledger for auditable production runs."""
from __future__ import annotations

import json
import os
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


# How much of a refusal's explanation is kept. Enough to diagnose a batch from its ledger alone,
# bounded so one pathological stack trace cannot dominate the file.
DETAIL_LIMIT = 1200


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
    # WHY THE DETAIL IS PART OF THE RECORD. `reason` is a category -- `could-not-specify`,
    # `no-probes-could-be-drawn` -- and a category cannot tell you what to fix. A refusal carries a
    # `detail` explaining the particular failure, and the ledger writer used to drop it, so a batch
    # that refused thirteen candidates left thirteen rows saying only which stage said no. Diagnosing
    # that batch meant guessing from the repository names, which is how a sourcing bug stayed
    # invisible: every row read as ordinary unusable material.
    detail: str = ""
    # WHAT THE CANDIDATE COST. Seconds alone does not say whether a long roll is affordable, and
    # the gateway reports usage on every reply. Counted from the replies themselves rather than
    # from a billing API, so it needs no admin key and no knowledge of prices.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0

    def __post_init__(self) -> None:
        if len(self.detail) > DETAIL_LIMIT:
            object.__setattr__(self, "detail", self.detail[:DETAIL_LIMIT])


class BatchLedger:
    """Crash-safe JSONL ledger; one record per candidate attempt.

    WHY APPENDING RATHER THAN REWRITING. An earlier version copied the whole file and renamed it over
    the original for every single row, to get atomicity. That is quadratic: the thousandth candidate
    rewrote nine hundred and ninety-nine rows, and a run large enough to be worth auditing spent more
    time on its own bookkeeping than on the work. It also loses the property it was buying -- a
    rename replaces the file, so a reader holding it open sees the old contents, and a crash between
    the copy and the rename can drop rows that were already durable.

    One `O_APPEND` write of one line is atomic enough for the guarantee that matters here: a row is
    either entirely present or entirely absent. That is a property of the write mode, not of the
    filesystem, provided the line fits in a pipe buffer -- which `DETAIL_LIMIT` above is what keeps
    true.
    """

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def append(self, record: LedgerRecord) -> None:
        value = asdict(record)
        value["timestamp"] = value["timestamp"] or datetime.now(timezone.utc).isoformat()
        line = (json.dumps(value, ensure_ascii=False) + "\n").encode()
        # Opened per call rather than held: a batch runs candidates concurrently, and a shared handle
        # would interleave two partial lines. `O_APPEND` makes each write land at the current end of
        # file, so concurrent writers cannot overwrite one another.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
