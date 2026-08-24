"""The one place a model is asked anything.

WHAT A MODEL IS ALLOWED TO DO HERE, and the boundary is the whole reason this module is small.
`core/sourcing.py` states one half: a model may rank and filter candidates, never produce them,
because a supply nobody can count is a supply whose yield means nothing. This is the other half --
a model may write a PROBE GENERATOR, which is code, and that code is never executed by this process.

    proposed              a generator, as text
    validated             it parses, and it defines the function the caller asked for
    executed              inside a container, by whoever supplied a runner
    used                  only its OUTPUT, as data

The package scale is why this exists. A single function has declarable parameter types, so its
probes can be sampled from a schema; a package's contract surface has dozens of entry points whose
valid inputs have nothing in common, and expressing that in a schema language would mean inventing
one badly. A generator is the honest answer, and a generator is code somebody has to write.

WHY THE GATEWAY IS OPENAI-SHAPED AND NOT A VENDOR SDK. One HTTP call with a JSON body, over
`urllib`, so that this package keeps its promise of no runtime dependencies -- and so that pointing
it at a different gateway is a change of URL rather than of code. Credentials come from
`credentials.get`, which is the only reader, and never from `os.environ` here.

NOTHING IN THIS MODULE DECIDES WHETHER A TASK IS GOOD. A model proposes inputs; what those inputs
SHOULD produce is answered by running the reference, exactly as for every other probe. That
separation is what keeps a model's judgement out of the expectations.
"""
from __future__ import annotations

import ast
import json
import urllib.error
import urllib.request

from . import credentials

# What a gateway is asked for when nothing else is said. Read from the environment so that a run can
# be pointed at a different model without editing anything.
DEFAULT_MODEL = "gpt-5.6-terra"

# Long enough for a model to write fifty lines, short enough that a hung gateway does not hold a
# batch. Sourcing has the same bound for the same reason.
TIMEOUT = 180.0


class ModelError(RuntimeError):
    """The gateway could not be reached, or answered something unusable.

    Distinct from a generator that is merely wrong: that is ordinary and is refused by validation
    with a message the caller can act on. This means the model could not be asked at all.
    """


def available() -> bool:
    """Whether a model could be asked here. Checked rather than assumed, like every other backend."""
    return bool(credentials.get("LLM_BASE_URL") and credentials.get("LLM_API_KEY"))


def ask(prompt: str, *, system: str = "", temperature: float = 0.2,
        model: str = "", timeout: float = TIMEOUT) -> str:
    """One completion. -> what the model said, as text.

    No streaming, no tools, no conversation. Everything this factory asks a model is one question
    with one answer, and a richer client would be surface area that nothing needs.
    """
    base = (credentials.get("LLM_BASE_URL") or "").rstrip("/")
    key = credentials.get("LLM_API_KEY")
    if not base or not key:
        raise ModelError(
            "no model gateway configured: set LLM_BASE_URL and LLM_API_KEY in the environment or "
            "in .env. See frf/core/credentials.py, which is the only reader of either.")

    messages = ([{"role": "system", "content": system}] if system else [])
    messages.append({"role": "user", "content": prompt})
    body = json.dumps({
        "model": model or credentials.get("LLM_MODEL") or DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
    }).encode("utf-8")

    request = urllib.request.Request(
        "%s/chat/completions" % base, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:300]
        except Exception:                                  # noqa: BLE001 -- the body is a bonus
            pass
        # The STATUS and the body, never the key. A diagnostic that echoes the Authorization header
        # publishes the credential into whatever log the message is pasted into.
        raise ModelError("the gateway answered %d: %s" % (error.code, detail)) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ModelError("the gateway did not answer: %s" % error) from error

    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as error:
        raise ModelError("the gateway answered in a shape this client cannot read: %s"
                         % json.dumps(payload)[:300]) from error


def code_from(answer: str) -> str:
    """The code out of a model's reply, whether or not it was fenced.

    Models fence code about half the time and explain it about a third of the time, and neither is
    worth failing over. What IS worth failing over is code that does not parse, which is checked by
    the caller -- this only removes the prose.
    """
    text = answer.strip()
    if "```" not in text:
        return text
    parts = text.split("```")
    # The fenced blocks are the odd-numbered parts. The first line of one may be a language tag.
    blocks = []
    for block in parts[1::2]:
        lines = block.splitlines()
        if lines and lines[0].strip().lower() in ("python", "py", "python3", "json"):
            lines = lines[1:]
        blocks.append("\n".join(lines))
    return max(blocks, key=len).strip() if blocks else text


def validated_generator(answer: str, *, function: str = "probes") -> str:
    """A model's reply -> a generator this factory is willing to hand to a container.

    THE VALIDATION IS SYNTACTIC AND NOTHING MORE, deliberately. It parses the source and checks that
    the named function is defined at the top level; it does not decide whether the generator is any
    good, because that question is answered by running it and looking at what comes back.

    Parsing is not executing. `ast.parse` builds a tree and runs none of it, which is what makes it
    safe to do here -- and it is the only inspection of model-written code that happens on this
    host at all.
    """
    source = code_from(answer)
    if not source.strip():
        raise ModelError("the model returned nothing that looked like code")
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ModelError("the generator does not parse: %s (line %s)"
                         % (error.msg, error.lineno)) from error

    defined = {node.name for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if function not in defined:
        raise ModelError(
            "the generator defines %s but not %r, which is the entry point the container calls"
            % (", ".join(sorted(defined)) or "nothing", function))
    return source
