"""Checkout-native Harbor task layout.

The subject is a complete real checkout. No source file is extracted, renamed or rewritten: local
imports, generated fixtures and the project's own build graph stay intact. The task's target scope
and verification commands are data from ``CheckoutContract``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from ..core import harbor, scratch, timing
from ..core.contract import CheckoutContract

_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "target", "build",
                                 "node_modules", ".frf-*")


def write(destination: str, package: harbor.Package, contract: CheckoutContract) -> str:
    """Emit a self-contained task from a real checkout and native test commands."""
    contract.validate()
    from ..core.source_tree import copy_tree
    harbor.write(destination, package)
    for room in (os.path.join(destination, "environment"),
                 os.path.join(destination, "tests", "reference")):
        copy_tree(contract.root, room, dirs_exist_ok=True, ignore=_IGNORE)
    manifest = {
        "target_paths": list(contract.target_paths),
        "build": [list(command) for command in contract.build],
        "verify": [list(command) for command in contract.verify],
        "benchmark": [list(command) for command in contract.benchmark],
        "min_speedup": contract.min_speedup,
        "timing_runs": contract.timing_runs,
        "contract": contract.to_json(),
    }
    with open(os.path.join(destination, "tests", "checkout-contract.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    with open(os.path.join(destination, "tests", "verify.py"), "w", encoding="utf-8") as handle:
        handle.write(_VERIFIER.replace('__TIMING_PROTOCOL__', timing.standalone_source()))
    return destination


def drive(path: str) -> tuple[int, int]:
    """Run a reference-vs-reference replay of the emitted task.

    Replay proves the source, hidden workload and verifier agree. It intentionally does not
    require the speed target: an unmodified reference is expected to be about 1x.
    """
    tests = os.path.join(path, "tests")
    with scratch.temporary_directory() as logs:
        reward = os.path.join(logs, "reward.json")
        done = subprocess.run(["python3", os.path.join(tests, "verify.py"),
                               "--task-root", tests,
                               "--workspace", os.path.join(path, "environment"), "--self-replay"],
                              capture_output=True, text=True, timeout=1800,
                              env=dict(os.environ, REWARD_PATH=reward))
        if not os.path.exists(reward):
            raise RuntimeError("checkout verifier produced no report: %s" %
                               (done.stderr or done.stdout)[-500:])
        report = json.load(open(reward, encoding="utf-8"))
    return int(report["correctness_passed"]), int(report["correctness_total"])


_VERIFIER = '''#!/usr/bin/env python3
import argparse, json, os, statistics, subprocess, time
__TIMING_PROTOCOL__

parser = argparse.ArgumentParser()
parser.add_argument("--task-root", required=True)
parser.add_argument("--workspace", required=True)
parser.add_argument("--self-replay", action="store_true")
args = parser.parse_args()
contract = json.load(open(os.path.join(args.task_root, "checkout-contract.json")))
reference = os.path.join(args.task_root, "reference")

def command_for(command, workspace):
    return [part.replace("{workspace}", workspace) for part in command]

def run(command, workspace, *, hidden=False):
    # A command containing {workspace} is a hidden evaluator: it runs from the reference
    # tree and receives the candidate root explicitly. Ordinary native build/test commands
    # retain their project's normal cwd semantics.
    cwd = reference if hidden else workspace
    return subprocess.run(command_for(command, workspace), cwd=cwd, capture_output=True,
                          text=True, timeout=900, env=dict(os.environ, NO_PROXY="*"))

commands = contract.get("build", []) + contract.get("verify", [])
passed = 0
for command in commands:
    try:
        done = run(command, args.workspace)
        passed += int(done.returncode == 0)
    except (OSError, subprocess.SubprocessError):
        pass

def timed_workload(workspace, index):
    t0 = time.perf_counter()
    done = run(contract['benchmark'][int(index)], workspace, hidden=True)
    elapsed = time.perf_counter() - t0
    if done.returncode:
        raise RuntimeError("workload failed: " + (done.stderr or done.stdout)[-500:])
    try:
        output = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("workload must write one JSON result to stdout: " + str(exc))
    return output, elapsed

benchmarks = contract.get("benchmark", [])
speedup = 1.0
timing_report = {}
timing_valid = False
note = "native checkout verification"
if benchmarks and passed == len(commands):
    try:
        shapes = [str(i) for i in range(len(benchmarks))]
        expected = {shape: timed_workload(reference, shape)[0] for shape in shapes}
        def cost(workspace, shape):
            out, elapsed = timed_workload(workspace, shape)
            if out != expected[shape]:
                raise RuntimeError('candidate or reference workload output differs from hidden reference')
            return elapsed
        measured = measure(lambda shape: cost(reference, shape),
                           lambda shape: cost(args.workspace, shape),
                           lambda shape, index: shape, shapes,
                           samples=max(SAMPLES_PER_SHAPE, int(contract.get('timing_runs', 7))))
        timing_report = measured.to_json()
        timing_report['cost_boundary'] = 'command wall time including process launch'
        if not measured.usable:
            raise RuntimeError(measured.note)
        speedup, note, timing_valid = measured.speedup, measured.note, True
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        passed = 0
        note = str(exc)
# Headroom is deliberately not a precondition. The design promises a measurable workload,
# not that every subject has a known optimization. Speedup is reported for the solver score.
correct = passed == len(commands)
report = {"correctness_passed": passed, "correctness_total": len(commands),
          "correct": correct, "reward": float(correct), "speedup": speedup, "note": note,
          "timing_valid": timing_valid, "timing": timing_report,
          "min_speedup": contract.get("min_speedup")}
reward = os.environ.get("REWARD_PATH")
if reward:
    json.dump(report, open(reward, "w"))
'''
