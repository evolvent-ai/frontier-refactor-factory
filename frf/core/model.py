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
import threading
import urllib.error
import urllib.request
import time
import os

from . import credentials

# What a gateway is asked for when nothing else is said. Read from the environment so that a run can
# be pointed at a different model without editing anything.
DEFAULT_MODEL = "gpt-5.6-terra"

# Long enough for a model to write fifty lines, short enough that a hung gateway does not hold a
# batch. Sourcing has the same bound for the same reason.
TIMEOUT = float(os.environ.get("FRF_LLM_TIMEOUT", "600"))

# THE STATUSES THAT DESCRIBE THE GATEWAY'S MOMENT RATHER THAN OUR REQUEST. A depleted balance is
# refilled, a rate limit window rolls over, a bad upstream recovers -- so the same body sent a
# moment later gets a different answer, and giving up on the first one throws away a candidate for
# a reason that had nothing to do with the candidate.
#
# THIS IS A THROUGHPUT PROPERTY, NOT A POLITENESS ONE. Over a long run the gateway's balance dips
# and resets on its own schedule; without this every dip kills whatever candidates were mid-flight.
# They are charged to `Fault.FACTORY` (pipeline.py classifies ModelError that way), so they do not
# corrupt the material yield -- but a batch that spends its budget re-sourcing after our outages
# measures our uptime instead of the supply.
#
# 402 payment required, 403 forbidden -- the balance gateways report either way; 408 request
# timeout; 409 conflict; 429 too many requests; and the 5xx family, which is by definition the
# gateway's own fault. Everything else -- 400 malformed, 401 bad key, 404 wrong URL -- is
# deterministic: the identical request fails identically for ever, and retrying it burns the
# caller's timeout to arrive at the same message three attempts later.
RETRYABLE_STATUS = frozenset((402, 403, 408, 409, 429))

# How many times one question may be asked. The real bound is the caller's `timeout`, which is a
# TOTAL budget -- this only stops a run of instant failures from spinning. Both are needed: the
# deadline alone would busy-loop on a gateway that refuses in a millisecond, and a count alone would
# let three slow attempts overrun a bounded roll.
MAX_ATTEMPTS = 6

# The ceiling on one backoff. A balance that resets on its own schedule is not waited out by
# doubling for ever; past this the caller's deadline is the thing that should decide.
MAX_BACKOFF = 30.0


def _retry_after(headers) -> float | None:
    """The gateway's own instruction, in seconds, or None.

    Only the delta-seconds form is read. `Retry-After` may also be an HTTP date, which is rarer from
    JSON gateways and whose parse failure would be silently indistinguishable from absence -- so an
    unreadable value falls back to the exponential schedule rather than being guessed at.
    """
    if headers is None:
        return None
    try:
        return max(0.0, float(str(headers.get("Retry-After") or "").strip()))
    except (TypeError, ValueError):
        return None


def _delay_for(attempt: int, headers=None) -> float:
    """How long to wait before the next attempt. Doubling, unless the gateway said otherwise."""
    told = _retry_after(headers)
    return min(told if told is not None else 2.0 ** attempt, MAX_BACKOFF)


def _wait(delay: float, deadline: float) -> bool:
    """Sleep before retrying. -> whether another attempt still fits inside the deadline.

    A delay that would outlast the budget is NOT slept through: waiting out a whole timeout to
    arrive at the same failure spends the caller's clock to learn nothing.
    """
    if delay >= deadline - time.monotonic():
        return False
    time.sleep(delay)
    return True


# WHAT EACH CANDIDATE COST, accumulated where the spending happens.
#
# A batch already records seconds per candidate and had no idea what it spent in tokens, which is
# the number that decides whether a long roll is affordable. The gateway returns `usage` on every
# reply and this module was discarding it along with the rest of the envelope.
#
# THREAD-LOCAL because candidates run concurrently in a pool and each writes its own ledger row; a
# module-level counter would blend one candidate's generator into another's. Nothing here reaches
# for a billing API: token counts come from the reply itself, so this works without an admin key
# and without knowing the gateway's prices.
_SPEND = threading.local()


def reset_usage() -> None:
    """Start counting again. Called once per candidate, before any asking."""
    _SPEND.prompt = 0
    _SPEND.completion = 0
    _SPEND.calls = 0


def usage_so_far() -> dict:
    """What this thread has spent since `reset_usage`. -> prompt/completion/calls."""
    return {"prompt_tokens": getattr(_SPEND, "prompt", 0),
            "completion_tokens": getattr(_SPEND, "completion", 0),
            "model_calls": getattr(_SPEND, "calls", 0)}


def _record_usage(payload: dict) -> None:
    """Add one reply's usage to this thread's running total.

    Absent or malformed usage is counted as a call with no tokens rather than raising: a batch must
    not fail because a gateway omitted an accounting field.
    """
    usage = payload.get("usage") if isinstance(payload, dict) else None
    _SPEND.calls = getattr(_SPEND, "calls", 0) + 1
    if not isinstance(usage, dict):
        return
    for key, field in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
        try:
            setattr(_SPEND, key, getattr(_SPEND, key, 0) + int(usage.get(field) or 0))
        except (TypeError, ValueError):
            pass


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
    # `timeout` is a total request budget, not a per-retry multiplier. Package candidates may ask
    # once for a generator and once for repair; three full waits per call can otherwise starve a roll.
    deadline = time.monotonic() + max(0.1, float(timeout))
    last_transport = None
    last_refusal = None
    payload = None
    for attempt in range(MAX_ATTEMPTS):
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        break
      try:
        with urllib.request.urlopen(request, timeout=remaining) as response:
            payload = json.loads(response.read())
        break
      except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:300]
        except Exception:                                  # noqa: BLE001 -- the body is a bonus
            pass
        # The STATUS and the body, never the key. A diagnostic that echoes the Authorization header
        # publishes the credential into whatever log the message is pasted into.
        last_refusal = "the gateway answered %d: %s" % (error.code, detail)
        if (error.code in RETRYABLE_STATUS or error.code >= 500) and attempt < MAX_ATTEMPTS - 1:
            if _wait(_delay_for(attempt, getattr(error, "headers", None)), deadline):
                continue
            break
        raise ModelError(last_refusal) from error
      except (urllib.error.URLError, TimeoutError, OSError) as error:
        last_transport = error
        if attempt < MAX_ATTEMPTS - 1 and _wait(_delay_for(attempt), deadline):
            continue
        raise ModelError("the gateway did not answer: %s" % error) from error
    if payload is not None:
        pass
    elif last_refusal is not None:
        # The last thing the gateway actually said, not a timeout message. "answered 402" names
        # something a person can act on; "exceeded its timeout" would hide it behind our own clock.
        raise ModelError(last_refusal)
    elif last_transport is not None:
        raise ModelError("the gateway did not answer: %s" % last_transport)
    else:
        raise ModelError("the model request exceeded its %.1fs total timeout" % float(timeout))

    _record_usage(payload)
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
