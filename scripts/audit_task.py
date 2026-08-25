#!/usr/bin/env python3
"""Deterministic task audit: Harbor schema plus reference self-replay."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from frf.core.harbor import validate_task_toml


def audit(task: Path, harbor_check: bool = False, harbor_timeout: int = 120) -> dict:
    task = task.resolve()
    errors = validate_task_toml((task / "task.toml").read_text())
    required = ("task.toml", "instruction.md", "environment", "tests/verify.py",
                "tests/test.sh", "tests/expectations.json")
    missing = [x for x in required if not (task / x).exists()]
    tests = task / "tests"
    reward = tests / ".audit-reward.json"
    env = dict(os.environ, REWARD_PATH=str(reward), SUBMISSION_ROOT=str(tests / "reference"))
    run = subprocess.run(["python3", str(tests / "verify.py"), "--task-root", str(tests),
                          "--workspace", str(tests / "reference")], cwd=str(tests), env=env,
                         capture_output=True, text=True, timeout=1800)
    report = json.loads(reward.read_text()) if reward.exists() else {}
    try: reward.unlink()
    except OSError: pass
    harbor = None
    if harbor_check:
        try:
            check = subprocess.run(["harbor", "check", str(task)], capture_output=True,
                                   text=True, timeout=harbor_timeout)
            harbor = {"returncode": check.returncode, "output":
                      (check.stdout + check.stderr)[-1000:]}
        except subprocess.TimeoutExpired:
            harbor = {"timeout": harbor_timeout}
        except OSError as exc:
            harbor = {"error": str(exc)}
    return {"task": str(task), "schema_errors": errors, "missing": missing,
            "verify_returncode": run.returncode,
            "correct": report.get("correct", False),
            "passed": report.get("correctness_passed", 0),
            "total": report.get("correctness_total", 0),
            "stderr": run.stderr[-500:], "harbor": harbor}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tasks", nargs="+", type=Path)
    parser.add_argument("--harbor-check", action="store_true")
    parser.add_argument("--harbor-timeout", type=int, default=120)
    args = parser.parse_args()
    results = [audit(path, args.harbor_check, args.harbor_timeout) for path in args.tasks]
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if all(not x["schema_errors"] and not x["missing"] and x["correct"]
                    and x["passed"] == x["total"] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
