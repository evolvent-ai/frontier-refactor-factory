"""What was established about one emitted task, written down where it can be audited later.

WHY THIS EXISTS. The evidence battery already ran on every task this factory has ever emitted, and
`emit()` already passed its verdicts down as `provenance["evidence"]`. But the writer only ever
rendered five provenance keys into a prose sentence, so the verdicts were computed, handed over, and
dropped on the floor. The consequence was not a wrong task -- it was that nothing downstream could
tell an audited task from an unexamined one, so every session re-derived the same answer by hand and
wrote the same "evidence still insufficient" note.

So the rule here is: a claim that is not written down did not happen. The record below is the only
thing entitled to promote a language/scale combination up the capability ladder, and it is
deliberately boring -- what ran, where it ran, what each check concluded, and when.

WHERE IT GOES, AND WHY NOT IN THE TASK. The public task layout (task.toml, instruction.md, tests/,
environment/) is a protocol shared with the harness, and this is factory bookkeeping rather than
part of the task. The full record is therefore a sidecar beside the task directory, while a short
summary goes into the `[metadata]` table that Harbor treats as free-form and does not validate. The
summary is what travels with a task that gets copied somewhere else; the sidecar is the detail.

WHAT AN ABSENT FIELD MEANS. "unrecorded" and "failed" are different facts and are never merged. A
check that did not run leaves its entry absent, and a reader that cannot find a field must report a
gap rather than assume a pass -- the same distinction `Outcome.INCONCLUSIVE` exists to preserve one
layer down.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone


# The sidecar directory, beside the emitted task rather than inside it. Dot-prefixed because it is
# ours: a consumer walking a batch for tasks should not find bookkeeping in the list.
DIRECTORY = ".frf-evidence"

SCHEMA = "frf-evidence/1"

# The `[metadata]` keys a summary is allowed to add. Enumerated rather than open-ended so that what
# lands in a public task file is a decision someone made once, not whatever a caller passed.
SUMMARY_KEYS = ("evidence_schema", "evidence_digest", "evidence_checks_held",
                "evidence_checks_total", "evidence_backend", "evidence_recorded_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build(*, name: str, scale: str, source_language: str, target_language: str = "",
          backend: str = "", verdicts: list | None = None, probes: int | None = None,
          graded_points: int | None = None, freeze_runs: int | None = None,
          discard_rate: float | None = None, adequacy=None, capability: dict | None = None,
          origin: str = "", extra: dict | None = None) -> dict:
    """Assemble the full record for one emitted task.

    `backend` is the name of the sandbox the observations were made in, and it is the field that
    decides what the record is worth: expectations frozen in this process describe this host, and a
    reader that cannot tell that from a container run cannot audit anything. It is recorded as given
    -- including empty -- rather than defaulted to something reassuring.

    `freeze_runs`, `probes`, `graded_points` and `discard_rate` are all optional and are OMITTED when
    not supplied rather than written as zero. A retroactive audit of an already-emitted task knows
    none of them, and "0 probes" is a claim about the corpus while "nobody recorded it" is the truth.
    The same reasoning applies to the shape a freeze returns: it belongs to its seam, and a scale may
    supply one that carries neither a run count nor a coverage audit.
    """
    checks = list(verdicts or ())
    held = sum(1 for v in checks if v.get("outcome") in ("holds", "not-applicable"))
    corpus = {}
    for key, value in (("probes", probes), ("graded_points", graded_points),
                       ("freeze_runs", freeze_runs)):
        if value is not None:
            corpus[key] = int(value)
    if discard_rate is not None:
        corpus["discard_rate"] = round(discard_rate, 4)
    record = {
        "schema": SCHEMA,
        "task": name,
        "scale": scale,
        "source_language": source_language,
        "target_language": target_language,
        "origin": origin,
        "backend": backend,
        "recorded_at": _now(),
        "corpus": corpus,
        "checks": checks,
        "checks_held": held,
        "checks_total": len(checks),
        "capability": dict(capability or {}),
    }
    if adequacy is not None:
        record["adequacy"] = adequacy
    if extra:
        record.update(extra)
    return record


def digest(record: dict) -> str:
    """A stable digest of the verdicts, so a summary can be matched against its sidecar.

    Over the checks alone rather than the whole record: the timestamp changes on every write, and a
    digest that changes when nothing about the evidence changed cannot be used to detect a task
    whose sidecar was swapped for another's.
    """
    payload = json.dumps(record.get("checks", []), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summary(record: dict) -> dict:
    """The few facts that travel inside the task file itself."""
    return {
        "evidence_schema": record.get("schema", SCHEMA),
        "evidence_digest": digest(record),
        "evidence_checks_held": int(record.get("checks_held", 0)),
        "evidence_checks_total": int(record.get("checks_total", 0)),
        # Empty rather than "local" when unknown: guessing here would be the one lie that matters.
        "evidence_backend": record.get("backend", ""),
        "evidence_recorded_at": record.get("recorded_at", ""),
    }


def path_for(destination: str, task_name: str) -> str:
    """Where one task's sidecar lives. `destination` is the batch directory, not the task."""
    safe = task_name.replace(os.sep, "_").replace("/", "_")
    return os.path.join(destination, DIRECTORY, safe + ".json")


def write(destination: str, record: dict) -> str:
    """Write one sidecar and return its path."""
    target = path_for(destination, str(record.get("task", "unnamed")))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".partial"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)          # a half-written record must never be readable as evidence
    return target


def read(path: str) -> dict:
    """One record, or {} when it is absent or unreadable. Absent is a gap, never a pass."""
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def collect(root: str) -> list[dict]:
    """Every record under `root`, at any depth.

    Any depth because roll mode nests a batch's output under `.candidates/<hash>/`, so the sidecars
    of a single run are not all in one directory.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) != DIRECTORY:
            continue
        dirnames[:] = []
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            record = read(os.path.join(dirpath, name))
            if record:
                found.append(record)
    return found


__all__ = ["DIRECTORY", "SCHEMA", "SUMMARY_KEYS", "build", "digest", "summary",
           "path_for", "write", "read", "collect"]
