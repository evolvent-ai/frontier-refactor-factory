#!/usr/bin/env python3
"""Record what can still be established offline about tasks that were emitted before evidence was
written down.

WHY A BACKFILL IS LIMITED BY CONSTRUCTION. The checks that decide whether a task WORKS -- the
reference reproduces its own expectations, a trivial submission does not, the emitted package
reproduces itself -- all require running the subject in a container. That cannot be reconstructed
after the fact from files on disk: it would mean rebuilding and re-freezing, which is a production run
rather than an audit. So this records only what a reader of the directory can verify: the schema and
the layout.

The consequence is deliberate. Every task this touches becomes `partial` in the census, never
`attested`. That is the honest answer -- these tasks were emitted by a factory that did run the full
battery on them and then discarded the verdicts, so the evidence is genuinely gone. Marking them
`attested` on the strength of a schema check would manufacture exactly the confidence that the missing
records cost us in the first place.

Writes a sidecar per task and adds a summary to `[metadata]`. Runs nothing, needs no credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import attestation, evidence, harbor                      # noqa: E402
from frf.core.capabilities import capability                            # noqa: E402


# What the emitted task can still be held to without running it. Named here rather than inline so the
# record says which checks were even attempted -- a reader must be able to tell a check that held from
# one that was never run.
OFFLINE_CHECKS = ("harbor-schema-valid", "layout-complete")


def _metadata(task_dir: str) -> dict:
    try:
        with open(os.path.join(task_dir, "task.toml"), "rb") as handle:
            return dict(tomllib.load(handle).get("metadata") or {})
    except (OSError, ValueError):
        return {}


def _layout_verdict(task_dir: str) -> evidence.Verdict:
    """The cheap structural invariants, as a verdict rather than a list of strings."""
    errors = harbor.deterministic_quality(task_dir)
    if errors:
        return evidence.Verdict("layout-complete", evidence.Outcome.FAILS,
                                "; ".join(errors[:3]))
    return evidence.Verdict("layout-complete", evidence.Outcome.HOLDS,
                            "instruction, environment, verifier and entry script are all present")


def audit_one(task_dir: str, batch_dir: str, *, apply: bool) -> dict:
    """Assemble (and optionally write) the record for one already-emitted task."""
    meta = _metadata(task_dir)
    verdicts = [evidence.harbor_schema_valid(os.path.join(task_dir, "task.toml")),
                _layout_verdict(task_dir)]
    language = str(meta.get("source_language", "") or "unknown")
    scale = str(meta.get("scale", "") or "unknown")

    record = attestation.build(
        name=os.path.basename(task_dir), scale=scale, source_language=language,
        target_language=str(meta.get("target_language", "") or ""),
        # NO BACKEND, because none was recorded and this audit did not run one. An empty string is the
        # only truthful value: claiming "local" would describe a container that never existed.
        backend="",
        verdicts=[v.to_json() for v in verdicts],
        capability=capability(language, scale=scale).__dict__,
        # probes/graded_points/freeze_runs are deliberately absent: the numbers exist in the emitted
        # expectations, but what they were frozen from is exactly what was not recorded, and a corpus
        # size copied out of the artefact would look like evidence about the freeze.
        extra={"audit": "offline-backfill",
               "audit_note": "recorded after emission; only checks that need no container were run",
               "checks_attempted": list(OFFLINE_CHECKS)})

    if apply:
        attestation.write(batch_dir, record)
        try:
            harbor.stamp_attestation(task_dir, attestation.summary(record))
        except (OSError, ValueError) as why:
            record["stamp_error"] = str(why)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="tasks")
    parser.add_argument("--apply", action="store_true",
                        help="write records; without it, only report what would be written")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        print("no such directory: %s" % args.root, file=sys.stderr)
        return 2

    held = failed = 0
    for dirpath, _dirnames, filenames in os.walk(args.root):
        if "task.toml" not in filenames or not _metadata(dirpath):
            continue
        record = audit_one(dirpath, os.path.dirname(os.path.abspath(dirpath)), apply=args.apply)
        ok = record["checks_held"] == record["checks_total"]
        held += ok
        failed += (not ok)
        if not ok:
            for check in record["checks"]:
                if check["outcome"] not in ("holds", "not-applicable"):
                    print("  %s: %s -- %s" % (os.path.relpath(dirpath, args.root),
                                              check["check"], check["detail"][:120]))

    print(json.dumps({"root": args.root, "applied": bool(args.apply),
                      "offline_checks_held": held, "offline_checks_failed": failed,
                      "note": "offline audit cannot establish attestation; these remain 'partial'"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
