"""Talking to a subject that is a separate program.

The subject -- reference or candidate -- is launched once and kept alive for the whole corpus, then
spoken to over its pipes. Once, not per probe, because a corpus is thousands of calls and a language
with a slow start (a JVM, a runtime that JITs) would otherwise be measured on its startup rather
than on its work, and the factory would spend its life on process creation.

Everything here is deliberately ignorant of what language the far side is: it receives a command to
run, and speaks JSON lines. That ignorance is the property that makes "any language" true rather
than aspirational -- there is nothing to extend when a new one arrives.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from .observation import Observation
from .protocol import Request, Response

# How long one call may take before the subject is presumed hung.
#
# WHY THIS IS SECONDS AND NOT MINUTES. A freeze runs every probe FIVE times, so a corpus of sixty
# costs three hundred calls, and the timeout is paid once per call in the worst case. At two minutes
# a single unresponsive candidate could hold a batch for ten hours; at ten seconds it costs fifty
# minutes at the very worst and almost always far less, because a subject that answers at all
# answers in milliseconds.
#
# The cost of being wrong in the other direction is small and visible: a genuinely heavy probe is
# refused as material this factory cannot time, which is an honest verdict and appears in the
# refusal log rather than as a batch that stopped.
DEFAULT_CALL_TIMEOUT = float(__import__("os").environ.get("FRF_CALL_TIMEOUT", "10"))


def _last_error(text: str, limit: int = 800) -> str:
    """The line that says what went wrong, out of whatever the subject printed.

    NEITHER THE HEAD NOR A FIXED TAIL. Taking the head reported a `dpkg` warning from the image as
    the cause of every failing candidate in a real batch. Taking a fixed tail cut the traceback
    mid-frame, so the report ended in `File "importlib/__init__.py", line 90` and never reached the
    `ModuleNotFoundError` that a reader actually needs.

    A traceback puts its exception LAST, so the last non-empty line is the finding, and the frame
    above it is the context worth keeping. Anything the environment printed first is neither.
    """
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "(empty)"
    culprit = lines[-1]
    # A crash dump does not always look like a Python traceback. Node's fatal error prints its
    # internal frames, then "Node.js v22.23.2" last -- so the last line says only which runtime and
    # the real cause sits a few lines up. The most actable line of a Node error is usually the
    # exception MESSAGE ("Cannot find module '...'"), not a stack frame, so search for it first.
    context = ""
    message_line = next((line.strip() for line in lines if (
        "Cannot find module" in line or "not defined" in line
        or "SyntaxError" in line or "is not a function" in line)), "")
    if message_line:
        # The finding is the END of this line -- the module path the runtime could not find.
        context = message_line[-760:]
    elif not any(line.startswith("File ") for line in lines[:-1]):
        tail_start = max(0, len(lines) - 4)
        tail = lines[tail_start:]
        if len(tail) >= 2 or culprit.startswith("Node.js"):
            context = " | ".join(line for line in tail[:-1])[-240:]
            for line in reversed(tail[:-1]):
                if "url:" in line:
                    context = line[-200:]
                    break

    joined = ("%s  [%s]" % (culprit, context)) if context else culprit
    return joined[:limit]


class SubjectFailed(RuntimeError):
    """The subject could not be spoken to at all -- as distinct from answering badly.

    The two are different findings and must not collapse: a subject that answers wrongly is a wrong
    submission, while a subject that never started is a broken environment or a broken build. Score
    them the same and the repair loop is sent to fix the wrong thing.
    """


class SubjectUnreachable(SubjectFailed):
    """We never got as far as running the subject: the sandbox died, the upload failed, the wire.

    THE WIRE IS NOT THE MATERIAL. A mute subject is ordinarily the candidate's doing -- a mined
    function whose file imports its own package cannot start beside a shim -- so freeze charges an
    unusable corpus to the material. A sandbox that vanished mid-push is the opposite: nothing was
    learned about the candidate at all, and filing it as bad material inflates the refusal record
    with our own outages. Since it is a subclass, existing `except SubjectFailed` handlers keep
    working; only the fault attribution differs.
    """


@dataclass
class Subject:
    """A live subject, held open across a corpus."""

    argv: list[str]
    cwd: str | None = None
    env: dict | None = None
    timeout: float = DEFAULT_CALL_TIMEOUT
    _proc: subprocess.Popen | None = None
    _next_id: int = 0

    def __enter__(self) -> "Subject":
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except OSError as exc:
            raise SubjectFailed("could not start %r: %s" % (self.argv[:2], exc)) from exc
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            self._proc.kill()
            self._proc.wait(timeout=10)
        finally:
            self._proc = None

    def _exchange(self, request: Request) -> Response:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise SubjectFailed("the subject is not running")
        try:
            self._proc.stdin.write(request.encode())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SubjectFailed("the subject closed its input: %s" % exc) from exc

        line = self._read_line()
        if line == "":
            # EOF: the subject exited. Its stderr is the only evidence of why, and a solver reading
            # a failure report needs it -- "the candidate crashed" without the message is a bug
            # report nobody can act on.
            stderr = ""
            if self._proc.stderr:
                try:
                    stderr = _last_error(self._proc.stderr.read())
                except OSError:
                    pass
            raise SubjectFailed("the subject exited without answering. stderr: %s"
                                % (stderr.strip() or "(empty)"))
        return Response.decode(line)

    def _read_line(self) -> str:
        """One reply line, or a failure if the subject takes too long over it.

        THE TIMEOUT WAS DECLARED AND NEVER APPLIED. `readline()` blocks for as long as the far side
        cares to think, so a subject that does not return on some input stops the whole batch --
        with no output, no error, and nothing to say which candidate did it. Found on a real run:
        a naive `fibonacci_recursion` mined from PyPI is exponential in its argument, and at the
        sizes the schema draws it simply never came back.

        A subject that is too slow to answer is the MATERIAL's fault and an ordinary outcome, so it
        arrives as `SubjectFailed` -- which the freeze stage already turns into an honest refusal.

        `select` rather than a thread or a signal: the pipe is the only thing being waited on, and
        the alternatives either need a reader per subject or interfere with whatever the caller has
        installed.
        """
        import select

        remaining = self.timeout
        chunks: list[str] = []
        while True:
            started = time.perf_counter()
            ready, _, _ = select.select([self._proc.stdout], [], [], max(0.0, remaining))
            if not ready:
                self._proc.kill()
                raise SubjectFailed(
                    "the subject did not answer within %.0fs, so it was stopped. A subject that "
                    "cannot answer a probe in that time cannot be graded on it." % self.timeout)
            chunk = self._proc.stdout.readline()
            if chunk == "":
                return "".join(chunks)
            chunks.append(chunk)
            if chunk.endswith("\n"):
                return "".join(chunks)
            # A partial line: the far side flushed mid-reply. Keep reading, but against the same
            # deadline, so a subject dribbling one byte at a time cannot hold the batch either.
            remaining -= time.perf_counter() - started
            if remaining <= 0:
                self._proc.kill()
                raise SubjectFailed("the subject stopped part-way through a reply line")

    def call(self, name: str, args: list) -> Observation:
        """One call -> what it produced. A refusal is an outcome, not an exception."""
        self._next_id += 1
        reply = self._exchange(Request(self._next_id, name, args))
        if reply.ok:
            return Observation(True, value=reply.value)
        return Observation(False, error=reply.error)

    def time(self, name: str, args: list, *, repeats: int = 1) -> float:
        """Seconds for `repeats` internal calls, self-measured on the far side.

        Self-measured because the alternative charges the subject for process startup and for this
        module's own JSON handling. On a subject whose real work takes a millisecond that overhead
        IS the measurement, and the resulting speedup would describe the transport.
        """
        self._next_id += 1
        started = time.perf_counter()
        reply = self._exchange(Request(self._next_id, name, args, op="time", repeats=repeats))
        if not reply.ok:
            raise SubjectFailed("the subject refused to time %r: %s" % (name, reply.error))
        # Falling back to the wall we measured here is honest but poor; it is better than reporting
        # zero, which would read as an infinitely fast candidate.
        return reply.seconds if reply.seconds > 0 else (time.perf_counter() - started)


# The ceiling on ONE batched call, however many probes it carries.
#
# `self.timeout * len(requests)` is the honest worst case -- the probes run one after another inside
# the sandbox -- and as a deadline it is useless: 120s x 57 probes is 114 minutes for a single
# freeze run, and freeze does five. A kernel/java candidate sat in exactly that, and the timeout it
# was given could not have fired before the sandbox holding it expired.
#
# THE ORDERING THAT HAS TO HOLD, and this is the same one `containers.SANDBOX_LIFETIME` states from
# the other side: an inner deadline must be strictly inside every outer one, or the outer one is
# what the caller actually gets. Here the outer bounds are the freeze budget and the sandbox
# lifetime, so a batch is capped below the smaller of them and a stuck subject is reported as a
# failed batch rather than as a container that vanished.
#
# The multiplication is kept BELOW the cap because a small batch should still get a tight bound;
# only the runaway case is clamped.
BATCH_TIMEOUT_CEILING = float(os.environ.get("FRF_BATCH_TIMEOUT_CEILING", "900"))


def _batch_timeout(per_probe: float, count: int) -> float:
    """How long one batched call may take. -> seconds, bounded.

    Bounded rather than proportional: see BATCH_TIMEOUT_CEILING. A batch that needs longer than the
    ceiling is one whose probes are individually slow, and splitting it is the caller's decision --
    silently waiting past the sandbox's own lifetime is not.
    """
    return min(max(per_probe, per_probe * max(1, count)), BATCH_TIMEOUT_CEILING)


class RemoteSubject:
    """A subject served INSIDE the sandbox, speaking the same line protocol.

    WHY THIS EXISTS AND WHY IT IS SHAPED DIFFERENTLY. `Subject` holds a live process open and
    exchanges one line at a time, which needs a persistent pipe. A sandbox backend offers no such
    pipe -- it runs a command and returns what it printed -- so the same conversation is held by
    writing EVERY request into a file, running the shim once over it, and reading the replies back
    in order.

    That is not a different protocol: it is the same JSONL, batched. The shim already reads stdin
    to exhaustion and answers line by line, which is what makes the batching possible without the
    far side knowing about it.

    WHY IT MATTERS. Without this, `backend='remote'` selected a sandbox and then froze the subject
    on the factory host anyway -- so an expectation described a program the shipped image would
    not contain, which is the one failure the design says freezing in the image exists to prevent.

    THE COST, stated rather than hidden: replies arrive only after the last request has run, so a
    subject that hangs on probe 3 costs the whole batch's timeout rather than one probe's. The
    per-request timeout is therefore enforced by the caller's overall bound, not per line.
    """

    def __init__(self, argv: list, *, workspace: str, backend,
                 remote_dir: str = "", timeout: float = DEFAULT_CALL_TIMEOUT) -> None:
        self.argv = list(argv)
        self.workspace = workspace
        self._backend = backend
        self._remote = remote_dir
        self.timeout = timeout
        self._pending: list = []
        self._replies: dict = {}
        self._next_id = 0

    def __enter__(self) -> "RemoteSubject":
        if not self._remote:
            import uuid
            self._remote = "/tmp/frf-subject-%s" % uuid.uuid4().hex[:12]
        try:
            # A call-seam subject's workspace is a package checkout, and its node_modules
            # and target/ are the dependency tree the package imports at runtime -- excluded
            # by the repo-scale default, where huge trees are not part of the contract.
            self._backend.push(self.workspace, self._remote,
                               exclude={'.git', '.hg', '__pycache__', '.pytest_cache', '.venv'})
        except Exception as exc:                       # noqa: BLE001 -- transport, not the subject
            raise SubjectUnreachable("could not stage the subject: %s" % exc) from exc
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        self._pending, self._replies = [], {}

    def _argv_in_sandbox(self) -> list:
        """The run argv with host paths rewritten to where the tree was pushed."""
        return [part.replace(self.workspace, self._remote) if isinstance(part, str) else part
                for part in self.argv]

    def ask(self, requests: list) -> dict:
        """Every request in one execution. -> {id: Response}.

        A request set is written as a file rather than echoed through a shell: quoting JSON through
        two layers of shell is how a harness ends up debugging its own quoting instead of the thing
        it is testing.
        """
        if not requests:
            return {}
        import os

        from ...core import scratch

        staging = scratch.mkdtemp(prefix="frf-requests-")
        try:
            with open(os.path.join(staging, "requests.jsonl"), "w", encoding="utf-8") as handle:
                for request in requests:
                    handle.write(request.encode())
            inbox = self._remote + "/inbox"
            try:
                self._backend.push(staging, inbox)
            except Exception as exc:                       # noqa: BLE001 -- transport, not the subject
                # AN EXPIRED SANDBOX IS NOT A MATERIAL FAILURE. The E2B sandbox has a lifespan,
                # and a long freeze (5 runs x hundreds of probes) can outlive it: the next push
                # raises TimeoutException ("sandbox was not found"), which used to escape `ask`
                # and take the whole candidate down as an unclassified crash. `call_many` catches
                # SubjectFailed and records the chunk as an honest failed probe set, so the freeze
                # gate can discard the corpus if enough of it is missing.
                raise SubjectFailed("could not stage requests into the sandbox: %s" % exc) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        argv = self._argv_in_sandbox()
        shell = "%s < %s/requests.jsonl" % (" ".join(argv), inbox)
        result = self._backend.run(["sh", "-c", shell], workdir=self._remote,
                                   timeout=_batch_timeout(self.timeout, len(requests)))
        if not result.ok and not (result.stdout or "").strip():
            raise SubjectFailed("the subject exited without answering. stderr: %s"
                                % (_last_error(result.stderr or "").strip() or "(empty)"))

        replies = {}
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                # The far side is someone else's program; a stray print is not a protocol error.
                continue
            reply = Response.decode(line)
            replies[reply.id] = reply
        return replies

    def call(self, name: str, args: list) -> Observation:
        """One call, as its own execution.

        Single-shot because `stages.freeze` asks probe by probe. Batching across probes would be
        faster and is what `ask` is for; it is not used here because doing so would change the
        order guarantees the freeze relies on.
        """
        self._next_id += 1
        request = Request(self._next_id, name, args)
        replies = self.ask([request])
        reply = replies.get(self._next_id)
        if reply is None:
            return Observation(False, error="no reply for request %d" % self._next_id)
        return Observation(True, value=reply.value) if reply.ok else Observation(False,
                                                                                 error=reply.error)

    def call_many(self, name: str, items: list[list]) -> dict[int, Observation]:
        """Send bounded JSONL batches, preserving response IDs.

        A single pathological input must not hold an entire 200-probe package freeze open. Chunks
        keep the remote command bound close to the per-call timeout while still removing the dominant
        one-E2B-command-per-probe overhead.
        """
        results = {}
        # Package freeze sends many small JSON requests. Sixteen keeps one pathological request
        # from monopolising a command while reducing E2B process round-trips by ~4x.
        chunk_size = 16
        for start in range(0, len(items), chunk_size):
            requests = []
            ids = []
            for args in items[start:start + chunk_size]:
                self._next_id += 1
                ids.append(self._next_id)
                requests.append(Request(self._next_id, name, args))
            try:
                replies = self.ask(requests)
            except SubjectFailed as failure:
                # Preserve the completed chunks; the missing chunk is a material refusal for its
                # probes and the caller's freeze gate will reject the resulting corpus honestly.
                for rid in ids:
                    results[rid] = Observation(False, error=str(failure)[:600])
                continue
            for rid in ids:
                reply = replies.get(rid)
                results[rid] = (Observation(True, value=reply.value)
                                if reply and reply.ok else
                                Observation(False, error=(reply.error if reply else
                                                          "no reply for request %d" % rid)))
        return results

    def time(self, name: str, args: list, *, repeats: int = 1) -> float:
        self._next_id += 1
        request = Request(self._next_id, name, args, op="time", repeats=repeats)
        reply = self.ask([request]).get(self._next_id)
        if reply is None or not reply.ok:
            raise SubjectFailed("the subject refused to time %r: %s"
                                % (name, reply.error if reply else "no reply"))
        return reply.seconds
