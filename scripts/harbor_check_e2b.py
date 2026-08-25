#!/usr/bin/env python3
"""Run Harbor quality checks with task-native E2B environments.

Harbor 0.22's built-in check wrapper forces ``python:3.13-slim`` and does not
carry the reviewed task's Dockerfile.  That is incompatible with our E2B-only
toolchain policy.  This adapter keeps Harbor's checker and rubric intact while
making the generated wrapper use the reviewed environment definition.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import tomllib
from pathlib import Path

from harbor.analyze import checker


_assemble = checker.assemble_check_task


def _assemble_with_environment(*args, **kwargs):
    wrapper = _assemble(*args, **kwargs)
    task_dir = Path(kwargs.get("task_dir", args[0] if args else ""))
    source = task_dir / "environment" / "Dockerfile"
    destination = wrapper / "environment" / "Dockerfile"
    if source.exists():
        shutil.copy2(source, destination)
        reviewed_environment = task_dir / "environment"
        wrapper_environment = wrapper / "environment"
        for item in reviewed_environment.iterdir():
            if item.name == "Dockerfile":
                continue
            target = wrapper_environment / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        dockerfile = destination.read_text()
        if "COPY task /app/task" not in dockerfile:
            destination.write_text("COPY task /app/task\n" + dockerfile)
        config_path = wrapper / "task.toml"
        config = tomllib.loads(config_path.read_text())
        environment = config.setdefault("environment", {})
        environment.pop("docker_image", None)
        environment["workdir"] = "/app"
        # Keep the wrapper artifact contract, while allowing E2B to build the image.
        lines = ["schema_version = \"1.4\"", "", "artifacts = [{ source = \"/app/check-result.json\", destination = \"check-result.json\" }]", ""]
        lines += ["[agent]", "timeout_sec = 1800.0", "", "[verifier]", "timeout_sec = 120.0", "", "[environment]"]
        for key, value in environment.items():
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            elif isinstance(value, (int, float)):
                lines.append(f"{key} = {value}")
        config_path.write_text("\n".join(lines) + "\n")
    return wrapper


checker.assemble_check_task = _assemble_with_environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task", type=Path)
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrent", type=int, default=1)
    args = parser.parse_args()
    report, _ = asyncio.run(checker.run_checks(
        args.task, agent=args.agent, model=args.model,
        environment=checker.EnvironmentType.E2B,
        n_concurrent=args.concurrent, n_attempts=1,
    ))
    print(report.model_dump_json(indent=2))
    return 0 if all(item.error is None for item in report.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
