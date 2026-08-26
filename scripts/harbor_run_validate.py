#!/usr/bin/env python3
"""Pipeline-external harbor run validation tool.

Runs `harbor run` against one or more emitted task directories to verify that
the full end-to-end task lifecycle works: environment builds, verifier executes,
and a reward.json is produced. Uses the solution/ directory as the submission
so the task's own reference proves it is solvable.

This is intentionally NOT part of the pipeline. It requires Docker and can take
minutes per task. Run it manually before a batch ships, or on e2b sandboxes for
CI-style coverage.

Usage:
    # Validate a single task
    python scripts/harbor_run_validate.py work/tasks/module/pgcd/

    # Validate all tasks in a directory
    python scripts/harbor_run_validate.py work/tasks/

    # Validate with e2b backend (runs each task in an isolated sandbox)
    python scripts/harbor_run_validate.py --backend e2b work/tasks/

    # Quick schema+check only (no Docker required)
    python scripts/harbor_run_validate.py --check-only work/tasks/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def find_task_dirs(root: Path) -> list[Path]:
    """Find all task directories under root (dirs containing task.toml)."""
    if (root / "task.toml").exists():
        return [root]
    return sorted(p.parent for p in root.rglob("task.toml"))


def schema_check(task_dir: Path) -> tuple[bool, str]:
    """Run harbor schema validation (no Docker needed)."""
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return False, "task.toml not found"
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from frf.core.harbor import validate_task_toml
        errors = validate_task_toml(toml_path.read_text())
        if errors:
            return False, "schema invalid: " + "; ".join(errors[:3])
        return True, "schema OK"
    except ImportError as exc:
        return False, "frf not importable: %s" % exc


def harbor_check(task_dir: Path, harbor_bin: str) -> tuple[bool, str]:
    """Run `harbor check <task-dir>` (no Docker needed)."""
    try:
        result = subprocess.run(
            [harbor_bin, "check", str(task_dir)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "harbor check timed out after 30s"
    except OSError as exc:
        return False, "could not execute harbor check: %s" % exc
    if result.returncode == 0:
        return True, "harbor check OK"
    output = (result.stdout + result.stderr).strip()
    first_fail = next((l for l in output.splitlines() if "FAIL" in l.upper()), output[:200])
    return False, "harbor check failed: %s" % first_fail


def harbor_run(task_dir: Path, harbor_bin: str, backend: str,
               timeout: int = 600, submission: Path | None = None) -> tuple[bool, str]:
    """Run `harbor run` against the task's solution/ directory as the submission."""
    if backend == "e2b":
        return False, ("Harbor CLI has no native --backend e2b mode; use "
                       "scripts/harbor_check_e2b.py for task-native E2B review")
    solution_dir = submission or (task_dir / "solution")
    if not solution_dir.exists():
        return False, "no solution/ directory to use as submission"

    # Harbor's current CLI has no --submission option. Stage an explicit submission into the
    # task layout it natively understands, keeping the source task untouched and cleaning up even
    # when the remote run fails.
    staging = None
    run_task = task_dir
    if submission is not None:
        staging = Path(tempfile.mkdtemp(prefix="frf-harbor-task-"))
        shutil.copytree(task_dir, staging / task_dir.name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("solution"))
        run_task = staging / task_dir.name
        shutil.copytree(solution_dir, run_task / "solution", dirs_exist_ok=True)

    cmd = [harbor_bin, "run", str(run_task)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "harbor run timed out after %ds" % timeout
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()[-400:]
        return False, "harbor run exited %d: %s" % (result.returncode, output)

    # Look for reward.json in the output
    output = result.stdout + result.stderr
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{") and "reward" in line:
            try:
                reward = json.loads(line)
                r = reward.get("reward", 0.0)
                correct = reward.get("correct", False)
                note = reward.get("note", "")
                if r >= 0.5 and correct:
                    return True, "reward=%.4f correct=%s note=%s" % (r, correct, note)
                else:
                    return False, "reward=%.4f correct=%s note=%s" % (r, correct, note)
            except json.JSONDecodeError:
                pass

    return True, "harbor run completed (no reward.json parsed from output)"


def find_harbor_bin() -> str | None:
    """Find harbor binary in PATH or local venv."""
    import shutil
    bin_path = shutil.which("harbor")
    if bin_path:
        return bin_path
    # Try local venv
    venv_bin = Path(__file__).parent.parent / ".venv" / "bin" / "harbor"
    if venv_bin.exists():
        return str(venv_bin)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path,
                        help="Task directory or parent directory containing tasks")
    parser.add_argument("--backend", default="local", choices=["local", "e2b"],
                        help="Execution backend for harbor run (default: local)")
    parser.add_argument("--check-only", action="store_true",
                        help="Run schema + harbor check only, skip harbor run (no Docker needed)")
    parser.add_argument("--schema-only", action="store_true",
                        help="Validate task.toml with the local schema checker only")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout in seconds for harbor run per task (default: 600)")
    parser.add_argument("--fail-fast", action="store_true",
                        help="Stop after the first failure")
    parser.add_argument("--skip-check", action="store_true",
                        help="Skip local `harbor check` before execution (useful with --backend e2b)")
    parser.add_argument("--submission", type=Path,
                        help="Explicit submission directory (default: <task>/solution)")
    args = parser.parse_args()

    harbor_bin = find_harbor_bin()
    if harbor_bin is None and not (args.check_only or args.schema_only):
        print("ERROR: harbor binary not found. Install with: pip install 'harbor>=0.21.0'")
        print("       or run with --check-only to skip harbor run")
        return 1

    # Collect all task dirs
    task_dirs: list[Path] = []
    for p in args.paths:
        task_dirs.extend(find_task_dirs(p))

    if not task_dirs:
        print("No task directories found (looking for dirs containing task.toml)")
        return 1

    print("Found %d task(s) to validate\n" % len(task_dirs))

    results: list[tuple[str, bool, str]] = []
    failed = 0

    for task_dir in task_dirs:
        label = str(task_dir)
        print("── %s" % label)
        t0 = time.monotonic()

        # Step 1: schema check (always)
        ok, msg = schema_check(task_dir)
        print("   schema:        %s  %s" % ("✅" if ok else "❌", msg))
        if not ok:
            results.append((label, False, "schema: " + msg))
            failed += 1
            if args.fail_fast:
                break
            continue

        # Step 2: harbor check (if harbor available)
        if harbor_bin and not args.schema_only and not args.skip_check:
            ok, msg = harbor_check(task_dir, harbor_bin)
            print("   harbor check:  %s  %s" % ("✅" if ok else "❌", msg))
            if not ok:
                results.append((label, False, "harbor check: " + msg))
                failed += 1
                if args.fail_fast:
                    break
                continue

        # Step 3: harbor run (unless --check-only)
        if not args.check_only and not args.schema_only and harbor_bin:
            ok, msg = harbor_run(task_dir, harbor_bin, args.backend, args.timeout,
                                 args.submission)
            elapsed = time.monotonic() - t0
            print("   harbor run:    %s  %s  (%.1fs)" % ("✅" if ok else "❌", msg, elapsed))
            if not ok:
                results.append((label, False, "harbor run: " + msg))
                failed += 1
                if args.fail_fast:
                    break
                continue

        elapsed = time.monotonic() - t0
        if args.schema_only:
            # Schema validation is deliberately weaker than a verifier/replay run.  Do not call
            # it a task result: downstream audit reports must not mistake protocol validity for
            # evidence that the reference executes correctly in an isolated environment.
            results.append((label, True, "schema-only validation passed (%.1fs)" % elapsed))
            print("   result:        ✅ schema-only (no execution)\n")
        elif args.check_only:
            results.append((label, True, "schema + harbor check passed (%.1fs)" % elapsed))
            print("   result:        ✅ checked (no execution)\n")
        else:
            results.append((label, True, "all checks passed (%.1fs)" % elapsed))
            print("   result:        ✅ passed\n")

    # Summary
    print("\n%s\n" % ("=" * 60))
    passed = len(results) - failed
    qualifier = " (schema-only; no execution)" if args.schema_only else (
        " (schema + harbor check; no execution)" if args.check_only else "")
    print("Results: %d passed, %d failed (of %d tasks)%s" %
          (passed, failed, len(results), qualifier))
    if failed:
        print("\nFailed tasks:")
        for label, ok, msg in results:
            if not ok:
                print("  ❌ %s — %s" % (label, msg))

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
