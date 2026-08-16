"""The shipped format: one description of a task, for all four scales.

There is exactly one emitter, and that is a decision worth defending because "we will share the
output format" is the kind of thing everyone agrees to and nobody keeps. It is kept here by the
interface: this module is handed a `Package` and never learns which scale produced it, so there is
no place for a scale-specific branch to grow.

WHAT A TASK IS, on disk:

    task.toml            what the harness reads: name, timeouts, whether a GPU is wanted
    instruction.md       what the solver reads
    environment/         the image the solver works in, plus the read-only reference tree
    tests/               the verifier, the expectations, the probes -- NEVER readable by the solver
    solution/            an oracle the harness can run to prove the task is solvable

THE VERIFIER'S DIRECTORY IS NOT THE SOLVER'S. `environment_mode = "separate"` is set here and is not
configurable, because the alternative has already shipped: with a shared environment `tests/` is
readable from inside the submission, and `tests/` holds a runnable reference and every expectation.
A submission that reads its own answer key and replays it scores full marks, and the instruction's
"do not call the reference" becomes a polite request rather than a property of the task.

Expectations therefore store DIGESTS, never values. Even with a separate environment: the rule costs
nothing to keep and removes a whole class of mistake, and a filesystem is one misconfiguration away
from being readable.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

SCHEMA_VERSION = "1.4"

# What the harness copies out of the solver's environment when it is done. The whole directory, not
# a named file: a submission is routinely a tree -- a compiled binary, a build directory, a package
# the entry point imports -- and declaring only the entry point carries the wrapper while leaving
# behind the program it runs.
ARTIFACTS = ["/app"]


@dataclass
class Package:
    """Everything needed to write one task, with nothing about how it was produced."""

    name: str
    scale: str                          # kernel | module | package | repo
    description: str                    # what the program IS, for someone who has never seen it
    instruction: str                    # the full task statement the solver reads
    source_language: str
    target_language: str = ""           # "" means optimise in place rather than reimplement
    provenance: dict = field(default_factory=dict)
    gpus: int = 0
    gpu_types: list = field(default_factory=list)
    agent_timeout_s: float = 14400.0
    build_timeout_s: float = 3600.0
    verifier_timeout_s: float = 3600.0

    @property
    def cross_language(self) -> bool:
        return bool(self.target_language) and self.target_language != self.source_language


def task_toml(package: Package) -> str:
    """The harness's view of the task.

    `gpus` and `gpu_types` are written for every task, zero and empty when the task is CPU-only.
    Emitting them always rather than only when nonzero means the day a GPU task exists, nothing
    about this file needs to change -- the fields are already the ones the harness reads.
    """
    lines = [
        'schema_version = "%s"' % SCHEMA_VERSION,
        "artifacts = %s" % json.dumps(ARTIFACTS),
        "",
        "[task]",
        'name = %s' % json.dumps("%s/%s" % (package.scale, package.name)),
        'description = %s' % json.dumps(package.description),
        'keywords = %s' % json.dumps(
            [package.scale, "cross-language" if package.cross_language else "optimisation"]),
        "",
        "[metadata]",
        'scale = %s' % json.dumps(package.scale),
        'source_language = %s' % json.dumps(package.source_language),
        'target_language = %s' % json.dumps(package.target_language),
        'cross_language = %s' % ("true" if package.cross_language else "false"),
        'provenance = %s' % json.dumps(_provenance_sentence(package)),
        "",
        "[environment]",
        "build_timeout_sec = %.1f" % package.build_timeout_s,
        'network_mode = "no-network"',
        'os = "linux"',
        "gpus = %d" % package.gpus,
        "gpu_types = %s" % json.dumps(package.gpu_types),
        "",
        "[agent]",
        "timeout_sec = %.1f" % package.agent_timeout_s,
        "",
        "[verifier]",
        "timeout_sec = %.1f" % package.verifier_timeout_s,
        "# SEPARATE, never shared. A shared environment makes tests/ readable from inside the",
        "# submission -- and tests/ holds the expectations and a runnable reference, so reading",
        "# the answer key and replaying it would score full marks.",
        'environment_mode = "separate"',
        "",
    ]
    return "\n".join(lines)


def _provenance_sentence(package: Package) -> str:
    """One sentence a reader can check the task against.

    Written into the shipped description rather than only into a side file, because this is the
    claim someone quotes months later: where the reference came from, and that nothing here was
    hand-authored.
    """
    origin = package.provenance.get("origin", "(origin not recorded)")
    probes = package.provenance.get("probes", 0)
    runs = package.provenance.get("freeze_runs", 0)
    return ("Reference is %s. The expectations are a frozen capture of that reference's own "
            "observable behaviour over %d probe(s), distilled from %d repeated runs so that only "
            "what it reproduces every time is graded. No expected output was hand-authored."
            % (origin, probes, runs))


def entry_script() -> str:
    """`tests/test.sh` -- what the harness actually executes.

    It runs the verifier and then flattens the detailed report into the flat `reward.json` the
    harness reads. Two files rather than one because the detailed report is what a human debugging a
    task needs, and the flat one is a contract with the harness; conflating them means either the
    harness sees fields it does not expect, or the detail is thrown away.
    """
    return '''#!/usr/bin/env bash
# Harbor entry point: run the verifier, then flatten its report into the reward.json the harness
# reads. Exits with the verifier's own status.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export REWARD_PATH=/logs/verifier/reward_detail.json
mkdir -p /logs/verifier

python3 "$HERE/verify.py" --task-root "$HERE" --workspace /app
rc=$?

python3 - "$REWARD_PATH" <<'PY'
import json, os, sys
source = sys.argv[1]
try:
    detail = json.load(open(source))
except Exception:
    # No report at all is a zero, and an HONEST zero: the verifier failed to answer, which is not
    # the same as the submission being wrong, so the note says which happened.
    detail = {"reward": 0.0, "note": "the verifier produced no report"}
flat = {
    "reward": float(detail.get("reward", 0.0) or 0.0),
    "correct": bool(detail.get("correct", False)),
    "correctness_passed": int(detail.get("correctness_passed", 0) or 0),
    "correctness_total": int(detail.get("correctness_total", 0) or 0),
    "speedup": float(detail.get("speedup", 0.0) or 0.0),
    "note": str(detail.get("note", "")),
}
json.dump(flat, open("/logs/verifier/reward.json", "w"), indent=2)
print("[test.sh] reward.json:", flat)
PY
exit $rc
'''


def write(destination: str, package: Package, *, instruction: str | None = None) -> None:
    """Lay out the task tree. Only the parts this module owns -- a scale adds tests/ and
    environment/ contents, because those are the parts that differ."""
    for sub in ("environment", "tests", "solution"):
        os.makedirs(os.path.join(destination, sub), exist_ok=True)
    _write(os.path.join(destination, "task.toml"), task_toml(package))
    _write(os.path.join(destination, "instruction.md"), instruction or package.instruction)
    entry = os.path.join(destination, "tests", "test.sh")
    _write(entry, entry_script())
    os.chmod(entry, 0o755)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
