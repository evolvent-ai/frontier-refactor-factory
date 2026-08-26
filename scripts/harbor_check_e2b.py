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
import os
import re
import shutil
import tomllib
from pathlib import Path

from harbor.analyze import checker

from frf.core import credentials


_assemble = checker.assemble_check_task


def _assemble_with_environment(*args, **kwargs):
    wrapper = _assemble(*args, **kwargs)
    task_dir = Path(kwargs.get("task_dir", args[0] if args else ""))
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", wrapper.name).strip("-") or "check-task"
    if safe_name != wrapper.name:
        safe_wrapper = wrapper.with_name(safe_name)
        wrapper.rename(safe_wrapper)
        wrapper = safe_wrapper
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
        if "go build" in dockerfile and "command -v go" not in dockerfile:
            # Some Harbor/E2B base-image aliases do not expose the toolchain expected by a
            # repository-native Dockerfile even when the nominal golang image is selected. Make
            # the temporary review wrapper self-healing; the emitted task remains untouched.
            marker = "\n"
            first_from = dockerfile.find("FROM ")
            line_end = dockerfile.find(marker, first_from)
            if first_from >= 0 and line_end >= 0:
                fallback = ("\nRUN command -v go >/dev/null 2>&1 || "
                            "(apt-get update && apt-get install -y --no-install-recommends golang-go "
                            "&& rm -rf /var/lib/apt/lists/*)\n")
                dockerfile = dockerfile[:line_end + 1] + fallback + dockerfile[line_end + 1:]
        if "go build" in dockerfile and "go mod download" not in dockerfile:
            # Harbor builds a fresh E2B template, so the Go module cache present during FRF's
            # reference build is not available. Resolve declared modules before the native build.
            build_marker = "RUN go build"
            dockerfile = dockerfile.replace(build_marker, "RUN go mod download\n" + build_marker, 1)
        if "COPY task /app/task" not in dockerfile:
            # COPY must follow FROM; placing it before the base image makes Docker ignore/reject
            # the reviewed environment and can surface as missing toolchains (e.g. go not found).
            destination.write_text(dockerfile.rstrip() + "\nCOPY task /app/task\n")
        config_path = wrapper / "task.toml"
        config = tomllib.loads(config_path.read_text())
        task = config.setdefault("task", {})
        if isinstance(task, dict) and task.get("name"):
            task["name"] = re.sub(r"[^A-Za-z0-9_-]+", "-", str(task["name"])).strip("-")
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


def repair_task(path: Path) -> bool:
    """Apply only mechanical, review-safe repairs; never touch verifier or expectations."""
    changed = False
    instruction = path / "instruction.md"
    if instruction.exists():
        text = instruction.read_text()
        additions = []
        if "## What you submit" not in text:
            additions.append("## What you submit\n\nCreate the required `/app/run.sh` entrypoint.")
        if "## Rules" not in text:
            additions.append("## Rules\n\nWork offline and do not access the reference or verifier artifacts.")
        if additions:
            instruction.write_text(text.rstrip() + "\n\n" + "\n\n".join(additions) + "\n")
            changed = True
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task", type=Path)
    # FRF's gateway exposes OpenAI Chat Completions. Codex CLI uses the Responses/WebSocket
    # protocol, so it can silently fall back to api.openai.com or hang on a Chat-Completions-only
    # gateway. Harbor's mini-swe-agent uses LiteLLM Chat Completions and is the compatible default;
    # callers may still select another reviewed agent explicitly.
    parser.add_argument("--agent", default="mini-swe-agent")
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrent", type=int, default=1)
    parser.add_argument("--job-name", default="", help="unique Harbor job name")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--repair-only", action="store_true")
    args = parser.parse_args()
    if args.repair:
        changed = repair_task(args.task)
        if args.repair_only:
            return 0 if changed else 2
    # Harbor agents use the OpenAI-compatible variable names, while FRF keeps credentials under
    # LLM_* so the factory can target any gateway. Forward only the two values required by the
    # agent sandbox; never write them into the task tree or logs.
    agent_env = {}
    # Harbor's E2B environment resolves its own credentials from process environment, while FRF
    # deliberately loads them through credentials.get() (including .env). Bridge that boundary
    # before checker.run_checks constructs the environment.
    e2b_key = credentials.get("E2B_API_KEY")
    e2b_template = credentials.get("E2B_DIND_TEMPLATE")
    if e2b_key:
        os.environ.setdefault("E2B_API_KEY", e2b_key)
    if e2b_template:
        os.environ.setdefault("E2B_TEMPLATE", e2b_template)
    llm_key = credentials.get("LLM_API_KEY")
    llm_base = credentials.get("LLM_BASE_URL")
    if llm_key:
        agent_env["OPENAI_API_KEY"] = llm_key
        # Harbor resolves the model connection in the host process before it creates the E2B
        # agent. Setting only agent_env is too late for Codex's generated config.toml.
        os.environ.setdefault("OPENAI_API_KEY", llm_key)
    if llm_base:
        agent_env["OPENAI_BASE_URL"] = llm_base
        # Different Codex builds have used different names while provider configuration was
        # stabilising. Supplying all aliases is harmless and keeps a custom OpenAI-compatible
        # gateway from silently falling back to api.openai.com.
        agent_env["OPENAI_API_BASE"] = llm_base
        agent_env["OPENAI_ENDPOINT"] = llm_base
        os.environ.setdefault("OPENAI_BASE_URL", llm_base)
        os.environ.setdefault("OPENAI_API_BASE", llm_base)
    model = args.model if "/" in args.model else "openai/" + args.model
    job_name = re.sub(r"[^A-Za-z0-9_-]+", "-", args.job_name).strip("-") or None
    report, _ = asyncio.run(checker.run_checks(
        args.task, agent=args.agent, model=model,
        environment=checker.EnvironmentType.E2B,
        n_concurrent=args.concurrent, n_attempts=1,
        agent_env=agent_env or None,
        job_name=job_name,
    ))
    print(report.model_dump_json(indent=2))
    return 0 if all(item.error is None for item in report.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
