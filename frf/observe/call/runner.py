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


def _last_error(text: str, limit: int = 400) -> str:
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
    context = next((line.strip() for line in reversed(lines[:-1])
                    if line.strip().startswith("File ")), "")
    joined = ("%s  [%s]" % (culprit, context)) if context else culprit
    return joined[:limit]


class SubjectFailed(RuntimeError):
    """The subject could not be spoken to at all -- as distinct from answering badly.

    The two are different findings and must not collapse: a subject that answers wrongly is a wrong
    submission, while a subject that never started is a broken environment or a broken build. Score
    them the same and the repair loop is sent to fix the wrong thing.
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
            self._backend.push(self.workspace, self._remote)
        except Exception as exc:                       # noqa: BLE001 -- transport, not the subject
            raise SubjectFailed("could not stage the subject: %s" % exc) from exc
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
        import tempfile

        staging = tempfile.mkdtemp(prefix="frf-requests-")
        try:
            with open(os.path.join(staging, "requests.jsonl"), "w", encoding="utf-8") as handle:
                for request in requests:
                    handle.write(request.encode())
            inbox = self._remote + "/inbox"
            self._backend.push(staging, inbox)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        argv = self._argv_in_sandbox()
        shell = "%s < %s/requests.jsonl" % (" ".join(argv), inbox)
        result = self._backend.run(["sh", "-c", shell], workdir=self._remote,
                                   timeout=self.timeout * max(1, len(requests)))
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
        chunk_size = 4
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
