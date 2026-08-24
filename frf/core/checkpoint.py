"""Append-only JSONL checkpoint for batch runs.

ATOMICITY. Each record is written to a temp file in the same directory, then renamed over. On any
POSIX filesystem rename is atomic at the filesystem level, so a record is either fully present or
absent -- a crash during the write leaves a temp file, not a corrupted record in the log. The temp
file is cleaned up on the next write.

CRASH SAFETY without atomicity would mean a partial JSON line could be in the file, making load_completed
blow up with a parse error on the next run. The rename avoids that.

SKIP-ALREADY-DONE. CheckpointWriter.load_completed() returns the set of identity strings for
candidates that were already processed, so build_async can skip them on resume.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass
class CheckpointRecord:
    identity: str
    scale: str
    task_form: str
    status: str     # "emitted" | "refused" | "error"
    stage: str
    reason: str
    fault: str
    timestamp: str  # ISO format
    path: str       # emitted task path or ""


class CheckpointWriter:
    """Append-only JSONL checkpoint file. Crash-safe."""

    def __init__(self, path: str) -> None:
        self._path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def write(self, record: CheckpointRecord) -> None:
        """Append one record atomically."""
        line = json.dumps(asdict(record), ensure_ascii=False) + "\n"
        dir_ = os.path.dirname(os.path.abspath(self._path))
        fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".ckpt-", suffix=".tmp")
        try:
            # Read the existing file content, append the new line, write all.
            existing = b""
            if os.path.exists(self._path):
                with open(self._path, "rb") as fh:
                    existing = fh.read()
            with os.fdopen(fd, "wb") as fh:
                fh.write(existing)
                fh.write(line.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_completed(self) -> set[str]:
        """Return set of identity strings already processed."""
        if not os.path.exists(self._path):
            return set()
        completed: set[str] = set()
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if "identity" in record:
                        completed.add(record["identity"])
                except json.JSONDecodeError:
                    pass
        return completed

    @property
    def path(self) -> str:
        return self._path


class CheckpointReader:
    """Read a checkpoint file and report stats."""

    def __init__(self, path: str) -> None:
        self._path = path

    def load_completed(self) -> set[str]:
        """Return set of identity strings already processed."""
        if not os.path.exists(self._path):
            return set()
        completed: set[str] = set()
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if "identity" in record:
                        completed.add(record["identity"])
                except json.JSONDecodeError:
                    pass
        return completed

    def _records(self) -> list[dict]:
        records = []
        if not os.path.exists(self._path):
            return records
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    def summary(self) -> dict:
        records = self._records()
        by_status: dict[str, int] = {}
        by_scale: dict[str, int] = {}
        by_stage: dict[str, int] = {}
        emitted_paths: list[str] = []
        for r in records:
            status = r.get("status", "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            scale = r.get("scale", "unknown")
            by_scale[scale] = by_scale.get(scale, 0) + 1
            if status in ("refused", "error"):
                stage = r.get("stage", "unknown")
                by_stage[stage] = by_stage.get(stage, 0) + 1
            if status == "emitted" and r.get("path"):
                emitted_paths.append(r["path"])
        return {
            "total": len(records),
            "by_status": by_status,
            "by_scale": by_scale,
            "by_stage": by_stage,
            "emitted_paths": emitted_paths,
        }

    def all_records(self) -> list[dict]:
        return self._records()


def make_checkpoint_path(prefix: str = "frf") -> str:
    """Generate an auto-named checkpoint path from current timestamp."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "%s-%s.jsonl" % (prefix, stamp)
