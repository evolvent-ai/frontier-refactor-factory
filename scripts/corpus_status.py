#!/usr/bin/env python3
"""What the corpus looks like right now: progress, spread, shape, format, quality.

`audit_matrix.py` answers "which scale/language cells have output". This answers the question a
person actually asks while a batch is running -- is it getting there, and is what it produces worth
having. Six things, because each can be healthy while another is not:

    PROGRESS   emitted against target, and the rate that implies
    LANGUAGES  a corpus that is 90% one language is not multi-language
    SOURCES    a corpus from four repositories is not diverse however many tasks it holds
    SHAPE      does each task match its scale -- a package task with one entry point is a module
               task wearing the wrong label
    FORMAT     the files a task must have, and whether task.toml parses as Harbor config
    QUALITY    attested vs partial, and which evidence checks are carrying it

Reads disk only: no container, no model, no network. Safe to run against a live batch.

    .venv/bin/python scripts/corpus_status.py <results-dir> [--target 25] [--watch 600]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import audit                                            # noqa: E402
from frf.core.diversity import repository_key                         # noqa: E402

SCALES = ("kernel", "module", "package", "repo")

# What every emitted task must carry, whatever its scale. Absent, the task cannot be run by the
# harness at all -- so this is a format check rather than a quality one.
REQUIRED = ("task.toml", "instruction.md", "tests", "environment")

# What each scale's shape looks like, as something checkable rather than asserted. A package task
# whose dispatch names one entry point is a module task with the wrong label; a repo task with no
# scenarios has nothing to observe on four channels.
SHAPE_RULES = {
    "package": ("tests/expectations.json", "entry_points", 2,
                "a package fans one entry out to many operations"),
    "repo": ("tests/scenarios.jsonl", None, 1,
             "a repo task is graded on lifted command scenarios"),
}


def _toml(path: str) -> dict:
    """The scalar fields of a task.toml, without a TOML parser for the common case."""
    found: dict = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition("=")
                key = key.strip()
                if key and "=" in line and key not in found:
                    found[key] = value.strip().strip('"')
    except OSError:
        pass
    return found


def _shape_of(task_dir: str, scale: str) -> tuple[bool, str]:
    """Whether the task looks like its scale. -> (ok, what was found)."""
    rule = SHAPE_RULES.get(scale)
    if rule is None:
        # kernel and module are one function by construction: the writer emits a single symbol.
        meta = _toml(os.path.join(task_dir, "task.toml"))
        return bool(meta.get("scale") == scale), "single-symbol"
    relative, key, minimum, _why = rule
    path = os.path.join(task_dir, relative)
    if not os.path.exists(path):
        return False, "missing %s" % relative
    try:
        if key:
            count = len(json.load(open(path, encoding="utf-8")).get(key) or ())
        else:
            count = sum(1 for line in open(path, encoding="utf-8") if line.strip())
    except (OSError, ValueError):
        return False, "unreadable %s" % relative
    return count >= minimum, "%d" % count


def _format_of(task_dir: str) -> list[str]:
    """Everything wrong with the task's files. Empty means nothing is."""
    problems = [name for name in REQUIRED if not os.path.exists(os.path.join(task_dir, name))]
    meta = _toml(os.path.join(task_dir, "task.toml"))
    if not meta.get("schema_version"):
        problems.append("no schema_version")
    if not meta.get("name"):
        problems.append("no name")
    # A name a person reads: no revision, no camel humps. The corpus this is measured against is
    # uniformly kebab-case, and mixed spelling in one set reads as three different producers.
    name = meta.get("name", "")
    leaf = name.rsplit("/", 1)[-1]
    if "@" in leaf:
        problems.append("revision in name")
    if leaf != leaf.lower():
        problems.append("mixed case in name")
    return problems


def survey(root: str, target: int) -> dict:
    subjects = audit.walk(root)
    rows = []
    for subject in subjects:
        if not subject.paths:
            continue
        path = subject.paths[0]
        meta = _toml(os.path.join(path, "task.toml"))
        shape_ok, shape_detail = _shape_of(path, meta.get("scale", subject.scale))
        rows.append({
            "scale": meta.get("scale", subject.scale),
            "language": meta.get("source_language", subject.language),
            "target_language": meta.get("target_language", ""),
            "cross": meta.get("cross_language", "false") == "true",
            "name": meta.get("name", os.path.basename(path)),
            "status": subject.status,
            "identity": subject.identity,
            # FROM THE AUDIT, NOT FROM THE TASK FILE. The status on the line above comes from the
            # audit, which prefers the sidecar record; reading the backend out of `[metadata]`
            # instead mixed two sources, so a task attested from its sidecar showed `backend=?` and
            # was then named as NOT WITNESSED REMOTELY. That is a false alarm about the most
            # safety-critical property here -- whether an expectation describes a sandbox or
            # whatever laptop froze it -- and it appeared against tasks that were witnessed
            # remotely, which is how a real one would get ignored.
            "held": subject.checks_held if subject.checks_total else meta.get("evidence_checks_held", "?"),
            "total": subject.checks_total or meta.get("evidence_checks_total", "?"),
            "backend": subject.backend or meta.get("evidence_backend", "") or "?",
            "shape_ok": shape_ok, "shape": shape_detail,
            "format": _format_of(path), "path": path,
        })
    return {"rows": rows, "target": target}


def report(data: dict) -> str:
    rows, target = data["rows"], data["target"]
    out = []
    attested = [r for r in rows if r["status"] == audit.ATTESTED]

    out.append("PROGRESS   %d/%d attested (%d subjects on disk)"
               % (len(attested), target * len(SCALES), len(rows)))
    for scale in SCALES:
        mine = [r for r in attested if r["scale"] == scale]
        langs = Counter(r["language"] for r in mine)
        bar = "#" * min(25, len(mine))
        out.append("  %-8s %3d/%-3d %-25s %s"
                   % (scale, len(mine), target, bar,
                      " ".join("%s:%d" % (k, v) for k, v in langs.most_common())))

    languages = Counter(r["language"] for r in attested)
    out.append("")
    out.append("LANGUAGES  %d distinct" % len(languages))
    if attested:
        top, count = languages.most_common(1)[0]
        share = 100.0 * count / len(attested)
        out.append("  %s" % "  ".join("%s:%d" % (k, v) for k, v in languages.most_common()))
        out.append("  most concentrated: %s at %.0f%% %s"
                   % (top, share, "-- thin" if share > 60 else "-- balanced"))

    sources = Counter(repository_key(r["identity"]) for r in attested)
    out.append("")
    out.append("SOURCES    %d repositories for %d tasks" % (len(sources), len(attested)))
    if sources:
        worst, worst_n = sources.most_common(1)[0]
        out.append("  most repeated: %s x%d %s"
                   % (worst.split(":")[-1][:44], worst_n,
                      "-- over the cap" if worst_n > 4 else ""))
        out.append("  tasks per repository: median %.1f, max %d"
                   % (statistics.median(sources.values()), max(sources.values())))

    out.append("")
    cross = [r for r in attested if r["cross"]]
    out.append("SHAPE      %d/%d match their scale; %d cross-language"
               % (sum(1 for r in attested if r["shape_ok"]), len(attested), len(cross)))
    for scale in ("package", "repo"):
        mine = [r for r in attested if r["scale"] == scale and r["shape"].isdigit()]
        if mine:
            counts = [int(r["shape"]) for r in mine]
            label = "entry points" if scale == "package" else "scenarios"
            out.append("  %-8s %s: median %d, range %d-%d"
                       % (scale, label, statistics.median(counts), min(counts), max(counts)))
    for row in (r for r in attested if not r["shape_ok"]):
        out.append("  WRONG SHAPE %s (%s)" % (row["name"], row["shape"]))

    out.append("")
    malformed = [r for r in rows if r["format"]]
    out.append("FORMAT     %d/%d clean" % (len(rows) - len(malformed), len(rows)))
    for row in malformed[:6]:
        out.append("  %s: %s" % (row["name"][:44], ", ".join(row["format"])))

    out.append("")
    statuses = Counter(r["status"] for r in rows)
    backends = Counter(r["backend"] for r in attested)
    out.append("QUALITY    %s" % "  ".join("%s:%d" % (k, v) for k, v in statuses.most_common()))
    out.append("  evidence: %s"
               % "  ".join(sorted({"%s/%s" % (r["held"], r["total"]) for r in attested})))
    out.append("  backend : %s" % "  ".join("%s:%d" % (k, v) for k, v in backends.most_common()))
    # NAMED, NOT COUNTED. "one task is not remote" sends a reader to grep; the name sends them to
    # the task. An expectation frozen anywhere but a sandbox describes the machine that froze it.
    local = [r for r in attested if r["backend"] != "remote"]
    for row in local:
        out.append("  NOT WITNESSED REMOTELY: %s (backend=%s) -- its expectations describe "
                   "whatever machine froze them" % (row["name"][:44], row["backend"]))
    return "\n".join(out)


def _rate(root: str) -> str:
    """Production rate from the ledgers, which carry timestamps the tasks do not."""
    stamps, emitted = [], 0
    for name in os.listdir(root):
        if not name.endswith(".ledger.jsonl"):
            continue
        for line in open(os.path.join(root, name), encoding="utf-8"):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("timestamp"):
                stamps.append(record["timestamp"])
            if record.get("status") == "emitted":
                emitted += 1
    if len(stamps) < 2 or not emitted:
        return ""
    import datetime
    span = (datetime.datetime.fromisoformat(max(stamps))
            - datetime.datetime.fromisoformat(min(stamps))).total_seconds() / 60
    return ("RATE       %d emissions over %.0f min of ledger span (%.1f min each)"
            % (emitted, span, span / emitted))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results")
    parser.add_argument("--target", type=int, default=25, help="tasks wanted per scale")
    parser.add_argument("--watch", type=int, default=0, help="repeat every N seconds")
    args = parser.parse_args()

    while True:
        alive = subprocess.run(["pgrep", "-f", "bin/frf run"], capture_output=True).returncode == 0
        print("=" * 78)
        print("%s   batch %s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "RUNNING" if alive else "NOT RUNNING"))
        print("=" * 78)
        print(report(survey(args.results, args.target)))
        rate = _rate(args.results)
        if rate:
            print("")
            print(rate)
        print("", flush=True)
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
