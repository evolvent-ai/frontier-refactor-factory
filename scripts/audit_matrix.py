#!/usr/bin/env python3
"""Census the emitted output: how many distinct subjects exist per language and scale, and what is
recorded about each.

Reads what is on disk and runs nothing. No container, no model call, no network -- so it is safe to
run at any point in a session and its answer does not depend on credentials being present.

THE UNIT IS A SUBJECT, NOT A FILE. Roll mode nests each candidate under `.candidates/<hash>/`, and a
subject re-frozen with a different probe count leaves another task directory behind. Counting
directories reported 105 tasks where there were 50 distinct subjects. Both numbers are printed, because
the gap between them is itself worth seeing.

WHAT IT WILL NOT TELL YOU. Whether a combination is certified. It reports what was recorded; deciding
whether that is enough is a judgement someone makes from this table, not from a summary line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import audit                                              # noqa: E402


def _table(report: dict) -> str:
    """The same facts as the JSON, laid out for a person."""
    lines = []
    width = max([len("%s/%s" % (c["scale"], c["language"])) for c in report["cells"]] + [12])
    lines.append("%-*s  %8s  %8s  %7s  %7s  %10s  %s"
                 % (width, "scale/language", "subjects", "attested", "partial", "failing",
                    "unrecorded", "backends"))
    for cell in report["cells"]:
        counts = cell["by_status"]
        lines.append("%-*s  %8d  %8d  %7d  %7d  %10d  %s"
                     % (width, "%s/%s" % (cell["scale"], cell["language"]),
                        cell["subjects"], counts[audit.ATTESTED], counts[audit.PARTIAL],
                        counts[audit.ATTESTED_FAILING], counts[audit.UNRECORDED],
                        ",".join(cell["backends"]) or "-"))
    totals = report["by_status"]
    lines.append("")
    lines.append("%d distinct subject(s) across %d task director(y/ies)"
                 % (report["subjects"], report["task_directories"]))
    lines.append("  attested %d, partial %d, attested-but-failing %d, unrecorded %d"
                 % (totals[audit.ATTESTED], totals[audit.PARTIAL],
                    totals[audit.ATTESTED_FAILING], totals[audit.UNRECORDED]))
    # "Attested" is deliberately hard to earn: it requires the three checks that cannot be obtained
    # without running the subject. Naming them here keeps the column honest for a reader who did not
    # read audit.py.
    lines.append("  attested = every recorded check held AND %s"
                 % ", ".join(audit.DECISIVE_CHECKS))
    absent = report["languages_without_output"]
    if absent:
        # A registered language with no output cannot be certified, and an empty cell is invisible in
        # a table built only from what exists.
        lines.append("  registered but no output at all: %s" % ", ".join(absent))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="tasks",
                        help="directory to census (default: tasks)")
    parser.add_argument("--json", action="store_true",
                        help="emit the full report as JSON instead of a table")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        print("no such directory: %s" % args.root, file=sys.stderr)
        return 2

    report = audit.report(args.root)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else _table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
