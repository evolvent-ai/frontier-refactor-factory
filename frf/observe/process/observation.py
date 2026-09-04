"""What one command produced, and how N of them become an Expectation.

An observation on this seam is what a PROCESS did: the code it exited with, what it wrote to each
stream, and what the directory looked like afterwards. Every program in every language has all four,
which is why nothing in this package branches on language.

The four are graded SEPARATELY. Collapsing them into one digest would make a task that differs only
in its exit code indistinguishable from one that differs only in its output, and the report a solver
gets back has to say which -- those are different bugs with different repairs.

WHY THIS FREEZE IS NOT THE OTHER ONE. Both seams run the reference N times and keep only what it
reproduces; they part company on what to do with the rest.

    this seam:  an ordered sequence of LINES. Line 7 carries a timestamp and varies every run, so
                line 7 is excluded and the other lines are still graded. The line number is a
                stable coordinate: a hole in the record keeps its meaning across runs.

    call seam:  a TREE, with no line 7 to exclude. There, an unstable probe is discarded whole.

Masking by position is only sound because the coordinate is stable, and it demands one thing in
return: THE LINE COUNT MUST MATCH BEFORE ANY MASK APPLIES. Otherwise deleting a line shifts every
later line up by one, the deleted content slides into the masked slot, and the mask hides the
deletion -- turning "we do not grade this line" into "you may remove a line for free".
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# The four things a process leaves behind. Named once, here, so that adding a fifth is a change in
# one place rather than a search for every tuple that happened to have four elements.
CHANNELS = ("exit_code", "stdout", "stderr", "tree")


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


@dataclass(frozen=True)
class Stream:
    """One text channel, kept as lines so that instability can be located rather than only detected."""

    lines: tuple[str, ...] = ()

    @classmethod
    def of(cls, text: str) -> "Stream":
        return cls(tuple(text.splitlines()))

    def digest(self, masked: frozenset[int] = frozenset()) -> str:
        """A fingerprint of the lines that are graded, with masked positions held out.

        The mask is applied by index rather than by removing the lines, so a masked line still
        occupies its position: the digest of a five-line stream with line 2 masked is not the digest
        of the four-line stream you get by deleting it.
        """
        kept = ["\x00" if i in masked else line for i, line in enumerate(self.lines)]
        return _digest("\n".join(kept))


@dataclass(frozen=True)
class Observation:
    """One command's outcome across all four channels."""

    exit_code: int
    stdout: Stream = field(default_factory=Stream)
    stderr: Stream = field(default_factory=Stream)
    tree: Stream = field(default_factory=Stream)

    def channel(self, name: str) -> Stream:
        if name not in CHANNELS:
            raise KeyError("unknown channel %r; expected one of %s" % (name, ", ".join(CHANNELS)))
        return getattr(self, name)


@dataclass(frozen=True)
class ChannelExpectation:
    """What the reference reproducibly does on ONE channel of one step."""

    digest: str
    line_count: int
    masked: frozenset[int] = frozenset()
    graded: bool = True
    reason: str = ""

    def to_json(self) -> dict:
        return {"digest": self.digest, "line_count": self.line_count,
                "masked": sorted(self.masked), "graded": self.graded, "reason": self.reason}

    @classmethod
    def from_json(cls, data: dict) -> "ChannelExpectation":
        return cls(digest=str(data.get("digest", "")), line_count=int(data.get("line_count", 0)),
                   masked=frozenset(data.get("masked") or ()),
                   graded=bool(data.get("graded", True)), reason=str(data.get("reason", "")))


@dataclass(frozen=True)
class Expectation:
    """What the reference reproducibly does for one step, over all four channels."""

    step: int
    exit_code: ChannelExpectation
    stdout: ChannelExpectation
    stderr: ChannelExpectation
    tree: ChannelExpectation

    def channel(self, name: str) -> ChannelExpectation:
        return getattr(self, name)

    def graded_points(self) -> int:
        return sum(1 for name in CHANNELS if self.channel(name).graded)

    def to_json(self) -> dict:
        out = {"step": self.step}
        out.update({name: self.channel(name).to_json() for name in CHANNELS})
        return out

    @classmethod
    def from_json(cls, data: dict) -> "Expectation":
        return cls(step=int(data["step"]),
                   **{name: ChannelExpectation.from_json(data[name]) for name in CHANNELS})


def _freeze_stream(runs: list[Stream]) -> ChannelExpectation:
    """N observations of one channel -> what may be graded, with unstable positions masked."""
    counts = {len(r.lines) for r in runs}
    if len(counts) > 1:
        # The line COUNT moved, so there is no position to mask: line 7 of one run is not line 7 of
        # another. Nothing here can be graded honestly.
        return ChannelExpectation("", 0, graded=False,
                                  reason="the reference produced %s different line counts across "
                                         "%d runs" % (sorted(counts), len(runs)))
    count = counts.pop() if counts else 0
    masked = frozenset(i for i in range(count)
                       if len({r.lines[i] for r in runs}) > 1)
    if count and len(masked) == count:
        # Every line moves. Grading a stream whose every position is masked would be grading nothing
        # while reporting a graded channel, which is the shape of a rubber stamp.
        return ChannelExpectation("", count, masked, graded=False,
                                  reason="every one of the %d lines varies between runs" % count)
    return ChannelExpectation(runs[0].digest(masked), count, masked)


def froze_work(steps: list) -> bool:
    """Whether the corpus that will SHIP shows the reference doing something. -> True if it does.

    `did_work` answers this about one raw observation; this answers it about the frozen consensus, and
    the two can disagree. The gate has to read this one, because this is what a submission is graded
    against -- judging the raw runs let a scenario be credited for a success that no expectation
    records.
    """
    empty = _digest("")
    for step in steps:
        out = step.channel("stdout")
        if out.graded and out.line_count > 0 and out.digest != empty:
            return True
        # A graded exit code of zero is work even with nothing on stdout: `fmt --write` rewrites files
        # and says nothing, and that is a perfectly gradable thing to reproduce.
        code = step.channel("exit_code")
        if code.graded and code.digest == _digest("0"):
            return True
    return False


def freeze(step: int, runs: list[Observation]) -> Expectation:
    """N observations of one step -> what may be graded on each of the four channels.

    Channels are frozen independently: a program with a clock in its stderr can still be graded on
    its exit code, its stdout and the files it wrote. Dropping the whole step because one channel
    moved would discard evidence that is perfectly reproducible.
    """
    if not runs:
        blank = ChannelExpectation("", 0, graded=False, reason="the reference never ran")
        return Expectation(step, blank, blank, blank, blank)

    codes = {r.exit_code for r in runs}
    exit_expectation = (
        ChannelExpectation(_digest(str(runs[0].exit_code)), 1)
        if len(codes) == 1 else
        ChannelExpectation("", 1, graded=False,
                           reason="the reference exited %s across %d runs"
                                  % (sorted(codes), len(runs))))

    return Expectation(step, exit_expectation,
                       *(_freeze_stream([r.channel(name) for r in runs])
                         for name in ("stdout", "stderr", "tree")))


def grade(expectation: Expectation, actual: Observation) -> tuple[int, int, list[str]]:
    """-> (passed, total, reasons). Each graded channel is worth exactly one point.

    A channel the freeze could not stabilise contributes to neither count -- it is not a point the
    submission failed, it is a point that does not exist.
    """
    passed = total = 0
    reasons = []
    for name in CHANNELS:
        expected = expectation.channel(name)
        if not expected.graded:
            continue
        total += 1
        if name == "exit_code":
            if _digest(str(actual.exit_code)) == expected.digest:
                passed += 1
            else:
                reasons.append("step %d exit_code: expected the frozen value, got %d"
                               % (expectation.step, actual.exit_code))
            continue

        stream = actual.channel(name)
        # THE LINE COUNT FIRST. Masking by position is only meaningful against a stream of the same
        # shape; comparing digests without this lets a deletion slide content into a masked slot.
        if len(stream.lines) != expected.line_count:
            reasons.append("step %d %s: expected %d line(s), got %d"
                           % (expectation.step, name, expected.line_count, len(stream.lines)))
            continue
        if stream.digest(expected.masked) == expected.digest:
            passed += 1
        else:
            ungraded = (" (%d line(s) not graded)" % len(expected.masked)) if expected.masked else ""
            reasons.append("step %d %s: %d line(s) match in count but differ in content%s"
                           % (expectation.step, name, expected.line_count, ungraded))
    return passed, total, reasons


def did_work(observed) -> bool:
    """Whether one observation shows the program having done something. -> bool.

    ONE DEFINITION, because two would drift and the drift would be silent. The smoke gate asks this
    of a single run before paying for a freeze; the freeze asks it of a whole corpus afterwards. If
    they disagreed, a corpus could pass the cheap gate and be discarded by the expensive one, or
    worse, the other way round.

    Stdout is a `Stream` of lines rather than a string: written for a string this answers "no work"
    for every observation there is, which would refuse everything.
    """
    stream = getattr(observed, "stdout", None)
    lines = getattr(stream, "lines", ()) if stream is not None else ()
    if any(str(line).strip() for line in lines):
        return True
    # A MISSING EXIT CODE IS NOT A SUCCESSFUL ONE, and `int(code or 0) == 0` said it was: `None or 0`
    # is `0`, so a run whose exit code was never captured -- a timeout, a killed process, a transport
    # failure mid-observation -- answered "this program did work".
    #
    # That is the whole leak. Eight repo tasks emitted with EVERY graded step showing empty stdout and
    # a non-zero exit, while the gate that exists to refuse exactly that stayed silent. The gate reads
    # `worked & set(frozen)` and refuses when that is empty, so its silence PROVES `worked` was not
    # empty -- something answered yes for scenarios whose frozen consensus was exit 2 with no output.
    # A run that never reported an exit code is the only thing that fits.
    #
    # `is not None` before the comparison, so absence is absence and only a real zero is success.
    code = getattr(observed, "exit_code", None)
    return code is not None and int(code) == 0
