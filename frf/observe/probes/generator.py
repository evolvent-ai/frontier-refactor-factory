"""Running a probe generator, which is model-written, where model-written code is allowed to run.

This is the one rule the pipeline does not bend: a generator is proposed by a model, and it is
executed inside a container. Never here. `Package(run_generator=...)` takes a callable so that the
choice of WHERE is made by the caller and is visible in the code that made it -- and this module is
the implementation that honours the rule.

WHY A GENERATOR AT ALL. A single function has declarable parameter types, so the module and kernel
scales sample their probes from a schema. A package's contract surface has dozens of entry points
whose valid inputs have nothing in common: expressing that in a schema language would mean inventing
a type language badly, and the honest alternative is code that builds the inputs.

WHAT COMES BACK IS DATA. The generator prints JSON to stdout; this reads it and validates the shape.
By the time anything crosses back into the factory it is a list of argument lists -- no objects, no
callables, nothing that could execute. That is what makes the boundary real rather than nominal:
there is no path by which the generator's code reaches this process.

WHAT IS NOT DECIDED HERE. What a probe SHOULD produce. The generator proposes inputs and never
answers; the expectation comes from running the reference, exactly as for every other probe.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

# How long a generator may run inside the container. Generous, because building two hundred inputs
# for a wide surface can be real work; bounded, because a generator that loops must not hold a batch.
TIMEOUT = 300.0

# What the container is asked to run. The generator is written beside it and imported, rather than
# concatenated into it, so a syntax error is reported against the generator's own line numbers.
_HARNESS = '''\
import json
import sys

sys.path.insert(0, ".")
from generator import probes                              # noqa: E402

drawn = probes(int(sys.argv[1]))

# ONE JSON DOCUMENT ON STDOUT AND NOTHING ELSE. A generator that prints while it works would
# otherwise corrupt its own output, so the payload is framed: the factory reads the last line.
sys.stdout.write("\\n" + json.dumps({"probes": list(drawn)}) + "\\n")
'''


class GeneratorFailed(RuntimeError):
    """The generator did not produce usable probes.

    The material's fault, not the wire's: a generator that raises, times out, or returns the wrong
    shape says something about this candidate. Distinguished from a container that could not be
    reached, which is ours and arrives as a `SandboxError`.
    """


def run_in(backend, source: str, count: int, *, room: str = "/tmp/frf-generator",
           timeout: float = TIMEOUT) -> list:
    """Execute a generator in `backend` and bring back its probes. -> list of argument lists.

    `backend` is a `core.sandbox.Backend`. Passing the local one is possible and is exactly the
    thing this module exists to discourage -- so the caller has to name it, and a reviewer can see
    that it was named.
    """
    staging = tempfile.mkdtemp(prefix="frf-generator-")
    try:
        with open(os.path.join(staging, "generator.py"), "w", encoding="utf-8") as handle:
            handle.write(source)
        with open(os.path.join(staging, "draw.py"), "w", encoding="utf-8") as handle:
            handle.write(_HARNESS)
        backend.push(staging, room)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    done = backend.run(["python3", "draw.py", str(count)], workdir=room, timeout=timeout)
    if not done.ok:
        raise GeneratorFailed("the generator failed inside the container: %s" % done.tail(1200))
    return _decode(done.stdout)


def _decode(stdout: str) -> list:
    """The generator's output -> argument lists, or a failure that says what was wrong.

    Validated rather than trusted, and the validation is not ceremony: a generator that returns
    `[1, 2, 3]` instead of `[[1], [2], [3]]` produces a corpus where every probe calls the subject
    with the wrong arity. Every probe then refuses identically, the freeze records a corpus of
    identical refusals, and the task looks healthy until somebody reads the expectations.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise GeneratorFailed("the generator printed nothing")
    try:
        payload = json.loads(lines[-1])
    except ValueError as error:
        raise GeneratorFailed("the generator's last line was not JSON: %r" % lines[-1][:200]) \
            from error

    drawn = payload.get("probes") if isinstance(payload, dict) else None
    if not isinstance(drawn, list) or not drawn:
        raise GeneratorFailed("the generator returned no probes")
    for index, args in enumerate(drawn):
        if not isinstance(args, list):
            raise GeneratorFailed(
                "probe %d is %s, not a list of arguments. A generator returns one ARGUMENT LIST "
                "per probe: [[1, 2], [3, 4]] is two probes of two arguments each, not one probe of "
                "two lists." % (index, type(args).__name__))
    return drawn
