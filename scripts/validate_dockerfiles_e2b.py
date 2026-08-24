#!/usr/bin/env python3
"""Validate all Dockerfile language templates by actually building them in e2b.

Uses an e2b sandbox with Docker-in-Docker (DinD) to run `docker build` against
every language combination we support. This is the definitive check that our
Dockerfile templates are correct — static analysis can't catch a bad download URL
or a missing apt package.

The e2b sandbox used here must be a custom template that has Docker daemon running.
Create it once with:

    e2b template build -f scripts/e2b-dind/Dockerfile --name frf-dind

Then set E2B_DIND_TEMPLATE to the returned template ID in your .env.

Usage:
    # Validate all language combos (runs in parallel, ~15-20min total)
    python scripts/validate_dockerfiles_e2b.py

    # Validate specific combos only
    python scripts/validate_dockerfiles_e2b.py --combos python:python python:rust go:go

    # Just check which combos exist without building
    python scripts/validate_dockerfiles_e2b.py --list

    # Re-run only failed combos from a previous run
    python scripts/validate_dockerfiles_e2b.py --retry-failed results.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Ensure frf is importable from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from frf.core.credentials import get
from frf.core.harbor import dockerfile_for
from frf.core.shims.dockerfiles import _LANGUAGE_SETUP

# Languages that make sense as cross-language optimization targets
# (faster than typical interpreted languages)
_FAST_LANGS = {"rust", "go", "c", "cpp", "zig", "nim", "crystal"}

# Languages that are common source languages in the wild
_COMMON_SOURCES = {
    "python", "javascript", "typescript", "ruby", "java",
    "go", "rust", "c", "cpp", "haskell", "scala", "kotlin",
    "elixir", "erlang", "swift", "r", "julia",
}

# Shell variants are only useful for repo-scale tasks; skip in cross-language matrix
_SKIP_AS_SOURCE = {"sh", "bash", "shell"}
_SKIP_AS_TARGET = {"sh", "bash", "shell"}


def all_combos() -> list[tuple[str, str]]:
    """Every (source, target) pair worth validating."""
    langs = sorted(k for k in _LANGUAGE_SETUP if k not in _SKIP_AS_SOURCE)
    result = []
    for src in langs:
        # inplace
        result.append((src, src))
        # cross-language: only to faster targets that differ from source
        if src in _COMMON_SOURCES:
            for tgt in sorted(_FAST_LANGS):
                if tgt != src and tgt not in _SKIP_AS_TARGET:
                    result.append((src, tgt))
    return result


def build_in_sandbox(src: str, tgt: str, api_key: str, template_id: str,
                     timeout: int = 600) -> tuple[bool, str]:
    """Build the (src, tgt) Dockerfile inside an e2b DinD sandbox.

    Returns (success, message).
    """
    try:
        dockerfile = dockerfile_for(src, tgt)
    except ValueError as exc:
        return False, "dockerfile_for raised: %s" % exc

    try:
        from e2b import Sandbox
        sbx = Sandbox.create(
            template=template_id,
            timeout=timeout,
            envs={"DEBIAN_FRONTEND": "noninteractive"},
            api_key=api_key,
        )
    except Exception as exc:
        return False, "sandbox create failed: %s" % exc

    try:
        # Write Dockerfile into the sandbox
        remote_dir = "/tmp/frf-build-%s-%s" % (src, tgt)
        sbx.commands.run("mkdir -p %s" % remote_dir, timeout=10)
        sbx.files.write("%s/Dockerfile" % remote_dir, dockerfile)

        # Run docker build (no cache, no push, just verify it builds)
        t0 = time.monotonic()
        result = sbx.commands.run(
            "docker build --no-cache --pull -t frf-test-%s-%s %s" % (src, tgt, remote_dir),
            timeout=timeout - 30,
        )
        elapsed = time.monotonic() - t0

        if result.exit_code == 0:
            # Also verify the toolchain is actually usable
            lang_cfg = _LANGUAGE_SETUP.get(tgt if tgt != src else src, {})
            verify_cmd = lang_cfg.get("verify_cmd", "")
            if verify_cmd:
                verify = sbx.commands.run(
                    "docker run --rm frf-test-%s-%s %s" % (src, tgt, verify_cmd),
                    timeout=60,
                )
                if verify.exit_code != 0:
                    return False, (
                        "build OK (%.0fs) but verify_cmd failed: %s"
                        % (elapsed, (verify.stdout + verify.stderr).strip()[-300:])
                    )
            return True, "build OK (%.0fs)" % elapsed
        else:
            output = (result.stdout + result.stderr).strip()
            return False, "docker build failed (%.0fs): %s" % (elapsed, output[-500:])

    except Exception as exc:
        return False, "runtime error: %s" % exc
    finally:
        try:
            sbx.kill()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--combos", nargs="*", metavar="SRC:TGT",
                        help="Specific combos to test, e.g. python:rust go:go")
    parser.add_argument("--list", action="store_true",
                        help="Print all combos and exit")
    parser.add_argument("--retry-failed", metavar="RESULTS_JSON",
                        help="Re-run only the combos that failed in a previous results file")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Max parallel sandbox builds (default: 8)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Per-build timeout in seconds (default: 600)")
    parser.add_argument("--output", metavar="RESULTS_JSON",
                        help="Write results to a JSON file")
    parser.add_argument("--template", metavar="TEMPLATE_ID",
                        help="e2b DinD template ID (overrides E2B_DIND_TEMPLATE env)")
    args = parser.parse_args()

    # Determine combos to test
    if args.list:
        combos = all_combos()
        print("All Dockerfile combos (%d):" % len(combos))
        for src, tgt in combos:
            mode = "inplace" if src == tgt else "cross"
            print("  %s:%s  [%s]" % (src, tgt, mode))
        return 0

    if args.retry_failed:
        with open(args.retry_failed) as fh:
            prev = json.load(fh)
        combos = [(r["source"], r["target"])
                  for r in prev.get("results", []) if not r.get("ok")]
        print("Retrying %d failed combos from %s" % (len(combos), args.retry_failed))
    elif args.combos:
        combos = []
        for spec in args.combos:
            if ":" not in spec:
                print("ERROR: combo must be SRC:TGT, got %r" % spec, file=sys.stderr)
                return 1
            src, tgt = spec.split(":", 1)
            combos.append((src, tgt))
    else:
        combos = all_combos()

    if not combos:
        print("No combos to test.")
        return 0

    # Credentials
    api_key = get("E2B_API_KEY")
    if not api_key:
        print("ERROR: E2B_API_KEY not set. Set it in .env or environment.", file=sys.stderr)
        return 1

    template_id = args.template or get("E2B_DIND_TEMPLATE") or os.environ.get("E2B_DIND_TEMPLATE")
    if not template_id:
        print(
            "ERROR: No DinD template ID. Create one with:\n"
            "  e2b template build -f scripts/e2b-dind/Dockerfile --name frf-dind\n"
            "Then set E2B_DIND_TEMPLATE in .env or pass --template <id>",
            file=sys.stderr,
        )
        return 1

    print("Validating %d Dockerfile combo(s) with %d parallel sandbox(es)...\n"
          % (len(combos), args.concurrency))

    results = []
    failed = 0
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(build_in_sandbox, src, tgt, api_key, template_id, args.timeout): (src, tgt)
            for src, tgt in combos
        }
        for future in as_completed(futures):
            src, tgt = futures[future]
            try:
                ok, msg = future.result()
            except Exception as exc:
                ok, msg = False, "unexpected error: %s" % exc

            label = "%s→%s" % (src, tgt)
            status = "✅" if ok else "❌"
            print("  %s  %-30s %s" % (status, label, msg))
            results.append({"source": src, "target": tgt, "ok": ok, "message": msg})
            if not ok:
                failed += 1

    elapsed = time.monotonic() - t_start
    passed = len(results) - failed

    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "elapsed_s": round(elapsed, 1),
        "results": sorted(results, key=lambda r: (not r["ok"], r["source"], r["target"])),
    }

    print("\n%s" % ("=" * 60))
    print("Results: %d passed, %d failed  (%.0fs total)" % (passed, failed, elapsed))

    if failed:
        print("\nFailed combos:")
        for r in summary["results"]:
            if not r["ok"]:
                print("  ❌ %s→%s — %s" % (r["source"], r["target"], r["message"]))

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(summary, fh, indent=2)
        print("\nResults written to %s" % args.output)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
