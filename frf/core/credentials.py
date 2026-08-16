"""The one place a secret is read.

Every other module asks here. That is not tidiness: a second reader is a second answer, and which
one you get depends on how the process happened to be launched. A run that works from a shell and
fails from a scheduler -- because one of them sourced a file the other did not -- costs more to
diagnose than this module costs to write.

Order is environment first, then `.env` beside the project. The environment wins so that a single
credential can be overridden for a single run without editing a file that is shared.

`.env` is never committed; `.env.example` lists the names and is. Nothing here ever logs a value:
the failure message names the KEY that is missing, because printing the secret to explain that the
secret is wrong publishes it into whatever collects the logs.
"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DOTENV = Path(os.environ.get("FRF_ENV_FILE") or (_ROOT / ".env"))

# Read once. A file that changes under a long run would make two stages disagree about which
# gateway they are talking to, and the resulting failure looks like a flaky network.
_cache: dict[str, str] | None = None


def _from_file() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    out: dict[str, str] = {}
    if _DOTENV.is_file():
        for line in _DOTENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    _cache = out
    return out


def get(name: str, *, default: str | None = None) -> str | None:
    """One credential, or `default` when it is set nowhere."""
    return os.environ.get(name) or _from_file().get(name) or default


def require(*names: str) -> dict[str, str]:
    """The named credentials, or a failure that says which ones are missing and where to put them.

    Raising here rather than returning None because every caller of this function needs all of what
    it asked for; a None that travels turns into a confusing failure much further along -- an
    authentication error from a gateway, or a container that dies at its most expensive stage.
    """
    found, missing = {}, []
    for name in names:
        value = get(name)
        if value:
            found[name] = value
        else:
            missing.append(name)
    if missing:
        raise LookupError(
            "missing credential(s): %s. Set them in the environment, or copy .env.example to %s "
            "and fill them in. Point FRF_ENV_FILE elsewhere to use a different file."
            % (", ".join(missing), _DOTENV))
    return found


def for_sandbox() -> dict[str, str]:
    """What a container needs, as environment.

    AS ENVIRONMENT, never as a pushed file. Writing the secrets into the sandbox's filesystem leaves
    them on a disk this process does not own, and they can come home again inside a pulled artefact.
    Passing them in the process environment keeps them to the life of the stage.

    Only what is set: a sandbox that does not need a gateway key should not be handed one.
    """
    return {k: v for k, v in ((n, get(n)) for n in
                              ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "GITHUB_TOKEN")) if v}
