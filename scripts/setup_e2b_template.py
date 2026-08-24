#!/usr/bin/env python3
"""One-time setup: build the DinD e2b template and write E2B_DIND_TEMPLATE to .env.

Run this once on a new machine before using any remote sandbox backend that needs Docker-in-Docker.

Usage:
    python scripts/setup_e2b_template.py

Prerequisites:
    - E2B_API_KEY must be set in the environment or in a .env file at the project root.
    - The e2b SDK must be installed: pip install e2b

The script:
    1. Reads E2B_API_KEY from the environment (or .env).
    2. Builds a new sandbox template from ubuntu:22.04 with Docker CE installed and dockerd
       configured to start automatically.
    3. Writes E2B_DIND_TEMPLATE=<id> to .env at the project root (appending or updating).
    4. Prints the template ID so it can be copied elsewhere if needed.
"""
from __future__ import annotations

import os
import re
import sys


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _load_dotenv(path: str) -> dict[str, str]:
    """Parse a .env file into a dict. Ignores comments and blank lines."""
    result: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return result


def _resolve_api_key(project_root: str) -> str:
    env_val = os.environ.get("E2B_API_KEY", "")
    if env_val:
        return env_val
    dotenv = _load_dotenv(os.path.join(project_root, ".env"))
    key = dotenv.get("E2B_API_KEY", "")
    if key:
        os.environ["E2B_API_KEY"] = key          # make it visible to the SDK
        return key
    return ""


# ---------------------------------------------------------------------------
# .env writer
# ---------------------------------------------------------------------------

def _write_env_var(env_path: str, key: str, value: str) -> None:
    """Insert or update a single key=value line in a .env file."""
    try:
        with open(env_path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []

    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    new_line = "%s=%s\n" % (key, value)
    updated = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            updated = True
            break
    if not updated:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(new_line)

    with open(env_path, "w") as fh:
        fh.writelines(lines)


# ---------------------------------------------------------------------------
# Template build
# ---------------------------------------------------------------------------

DOCKER_INSTALL_CMDS = [
    "apt-get update -qq",
    (
        "apt-get install -y --no-install-recommends "
        "ca-certificates curl gnupg lsb-release"
    ),
    "install -m 0755 -d /etc/apt/keyrings",
    (
        "curl -fsSL https://download.docker.com/linux/ubuntu/gpg "
        "| gpg --dearmor -o /etc/apt/keyrings/docker.gpg"
    ),
    "chmod a+r /etc/apt/keyrings/docker.gpg",
    (
        'echo "deb [arch=$(dpkg --print-architecture) '
        "signed-by=/etc/apt/keyrings/docker.gpg] "
        "https://download.docker.com/linux/ubuntu "
        '$(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list'
    ),
    "apt-get update -qq",
    (
        "apt-get install -y --no-install-recommends "
        "docker-ce docker-ce-cli containerd.io docker-buildx-plugin "
        "python3 python3-pip"
    ),
    "rm -rf /var/lib/apt/lists/*",
]


def build_template() -> str:
    """Build the DinD template and return the template ID."""
    try:
        from e2b import Template
    except ImportError:
        print("error: the e2b package is not installed — run: pip install e2b", file=sys.stderr)
        sys.exit(1)

    print("Building DinD e2b template from ubuntu:22.04 ...")
    print("This takes a few minutes the first time.\n")

    builder = Template().from_ubuntu_image("22.04").set_user("root")

    for cmd in DOCKER_INSTALL_CMDS:
        print("  run: %s" % cmd[:80])
        builder = builder.run_cmd(cmd)

    # Start dockerd in the background and verify it responds before any user command.
    builder = builder.set_start_cmd(
        "dockerd &>/var/log/dockerd.log 2>&1 &",
        "docker info",
    )

    template_id: str = builder.build()
    return template_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(project_root, ".env")

    api_key = _resolve_api_key(project_root)
    if not api_key:
        print(
            "error: E2B_API_KEY is not set.\n"
            "\n"
            "Set it in one of these ways:\n"
            "  1. Export it in your shell:  export E2B_API_KEY=e2b_...\n"
            "  2. Add it to .env at the project root:  E2B_API_KEY=e2b_...\n"
            "\n"
            "Get your key at https://e2b.dev/dashboard",
            file=sys.stderr,
        )
        return 1

    print("E2B_API_KEY found (not shown).\n")

    template_id = build_template()

    _write_env_var(env_path, "E2B_DIND_TEMPLATE", template_id)

    print("\nDone.")
    print("Template ID : %s" % template_id)
    print("Written to  : %s" % env_path)
    print("\nE2B_DIND_TEMPLATE=%s" % template_id)
    print(
        "\nTo use it manually, export the variable:\n"
        "  export E2B_DIND_TEMPLATE=%s" % template_id
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
