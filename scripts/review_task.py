#!/usr/bin/env python3
"""Review one emitted task before it can enter an example or release corpus."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.audit_public_delivery import audit
from frf.core.harbor import validate_task_toml
from frf.observe.artifact_contract import contract_findings

REQUIRED = ("instruction.md", "task.toml", "environment", "tests")
SECTIONS = ("## Task Facts", "## Workspace", "## Build & Test", "## Constraints",
            "## Submission Contract", "## Time Budget", "## Behavioral Rules", "## Grading Signals")



def review(root: str) -> dict:
    path = Path(root)
    findings = []
    missing = [name for name in REQUIRED if not (path / name).exists()]
    for name in missing:
        findings.append({"kind": "missing", "path": name})
    instruction = (path / "instruction.md").read_text(encoding="utf-8") if (path / "instruction.md").is_file() else ""
    task = (path / "task.toml").read_text(encoding="utf-8") if (path / "task.toml").is_file() else ""
    try:
        config = tomllib.loads(task)
    except tomllib.TOMLDecodeError:
        config = {}
    if not config.get("task") or not config.get("environment") or not config.get("verifier"):
        findings.append({"kind": "invalid-harbor-config", "detail": "missing Harbor task/environment/verifier sections"})
    for error in validate_task_toml(task):
        findings.append({"kind": "invalid-harbor-config", "detail": error})
    metadata = config.get("metadata", {})
    for section in SECTIONS:
        if not re.search(r"(?m)^" + re.escape(section) + r"\s*$", instruction):
            findings.append({"kind": "instruction-missing-section", "section": section})
    title = instruction.splitlines()[0] if instruction else ""
    task_name = config.get("task", {}).get("name", "")
    task_slug = task_name.rsplit("/", 1)[-1]
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', task_slug):
        findings.append({'kind': 'invalid-task-name'})
    if title.startswith("# ") and title[2:].strip() == task_slug:
        findings.append({"kind": "title-is-slug"})
    scale = metadata.get("scale", "")
    if not scale and "/" in task_name:
        scale = task_name.split("/", 1)[0]
    form = "cross" if metadata.get("cross_language") else "inplace"
    if scale == "repo":
        scenarios = path / "tests" / "scenarios.jsonl"
        if not scenarios.is_file() or not scenarios.read_text().strip():
            findings.append({"kind": "repo-no-scenarios"})
    if scale in {"package", "module", "kernel"} and not (path / "tests" / "expectations.json").exists():
        findings.append({"kind": "call-no-expectations"})
    findings.extend(contract_findings(path, dict(metadata, scale=scale)))
    delivery = audit(root)
    return {"task": path.name, "scale": scale, "form": form,
            "findings": findings, "public_delivery": delivery,
            "ok": not findings and delivery["ok"], "check_type": "static-preflight",
            "release_ready": False,
            "runtime_required": ["offline-build", "replay", "solver-isolation"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--json")
    args = parser.parse_args()
    report = review(args.task)
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
