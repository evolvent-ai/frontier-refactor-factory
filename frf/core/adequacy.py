"""Did the corpus measure the right thing?

Evidence asks whether the verifier is honest. This asks the orthogonal question, and the two are
confused often enough to be worth stating as an experiment: a corpus containing only `git --version`
passes the ENTIRE evidence battery. The reference reproduces it, an empty submission scores zero,
every channel bites, the emitted package replays itself. Eight green checks, and the task tests
nothing at all.

So adequacy is not a stricter kind of evidence; it is a different axis, and a task needs both.

TWO NUMBERS, NEITHER SUFFICIENT ALONE.

    REACH   how much of the subject the corpus executes. Code that never runs is code a submission
            can get arbitrarily wrong with no observation moving.
    FLOOR   what a submission that does nothing at all already scores. Points a blank submission
            collects are points that distinguish nothing.

High reach with a high floor means the corpus runs the program and grades almost none of what it
does. A low floor with low reach means what little is graded is hard to guess, and most of the
program is untested. Only both together say anything.

FLOOR IS MEASURED AGAINST SEVERAL TRIVIAL SUBMISSIONS AND THE WORST IS TAKEN. Trying one is trying
your luck: on the same task, exiting 0 and exiting 1 have scored 50% and 75%. Whichever a solver
would stumble into first is the one that matters, so the maximum is the honest figure.

THE REPAIR LOOP IS THE POINT. A number that only rejects is worth much less than one that says where
to look, so an inadequate corpus is not merely refused -- the unreached regions are reported, a
model proposes inputs aimed at them, the corpus is re-frozen, and the measurement runs again. The
model proposes inputs and never verdicts: what a new probe SHOULD produce is answered by running the
reference, exactly as for every other probe.

COVERAGE IS A REPORT, NOT A GATE. A language nobody has written a backend for still ships tasks; it
ships them with one fewer number and says so. Pretending every language can be instrumented would
either restrict which languages this factory serves or invite a fabricated figure, and both are
worse than an honest absence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

# Reach below this means the corpus barely runs the subject at all. Deliberately a low bar: it
# exists to reject a corpus that tests nothing, not to certify one that tests everything. A
# repository-scale subject can have a hundred thousand executable lines that its command-line
# surface can never reach, and demanding a high fraction there would reject exactly the large real
# programs this factory most wants.
MIN_REACH = 0.25

# Above this, most of the score is available to a submission that does nothing. Whatever else is
# true of such a corpus, it cannot separate a real implementation from a lucky one.
MAX_FLOOR = 0.75


class CoverageBackend(Protocol):
    """Measures which lines of the subject a corpus executed.

    Per language, and genuinely per language: reading which lines ran is not something a wire
    protocol can abstract. The null backend below is what a language without one gets.
    """

    name: str

    def measure(self, subject, probes) -> "Reach": ...


@dataclass(frozen=True)
class Reach:
    """How much of the subject ran, and which parts did not.

    `dark` is the half that makes this actionable. Reporting only the fraction gives a repair loop
    nothing to aim at, and "your corpus is thin" without "here is what it never touched" has been
    measured to recover almost nothing.
    """

    reached: int = 0
    total: int = 0
    dark: tuple = ()
    backend: str = "none"

    @property
    def measured(self) -> bool:
        """False means no backend, which is different from measuring zero.

        A backend that ran and found nothing executed is a BROKEN measurement -- the tracer did not
        attach, the paths were wrong -- and reporting that as "unmeasured" is how a 0/0 sails
        through a gate. The distinction is the caller's to make and this is where it is named.
        """
        return self.total > 0

    @property
    def fraction(self) -> float:
        return (self.reached / self.total) if self.total else 0.0

    def to_json(self) -> dict:
        return {"backend": self.backend, "reached": self.reached, "total": self.total,
                "fraction": round(self.fraction, 4), "measured": self.measured,
                "dark": list(self.dark[:20])}


class NullCoverage:
    """What a language without a backend gets: an honest absence.

    Not an error, and not a zero. A task in such a language ships with one fewer quality number and
    a statement saying which, because the alternative is to restrict the languages this factory
    serves to those somebody has instrumented.
    """

    name = "none"

    def measure(self, subject, probes) -> Reach:
        return Reach(backend=self.name)


@dataclass(frozen=True)
class Floor:
    """What a submission that does nothing already scores, and which attempt found it."""

    fraction: float = 0.0
    worst: str = ""
    attempts: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"fraction": round(self.fraction, 4), "worst": self.worst,
                "attempts": {k: round(v, 4) for k, v in self.attempts.items()}}


def measure_floor(score_trivial: Callable[[str], tuple[int, int]], names: tuple) -> Floor:
    """Score several do-nothing submissions and keep the WORST outcome.

    Worst rather than first: whichever a solver stumbles into is the one that decides what the task
    is worth, and on one measured task `exit 0` scored 50% where `exit 1` scored 75%.
    """
    attempts = {}
    for name in names:
        passed, total = score_trivial(name)
        attempts[name] = (passed / total) if total else 0.0
    worst = max(attempts, key=lambda k: attempts[k]) if attempts else ""
    return Floor(attempts.get(worst, 0.0), worst, attempts)


@dataclass(frozen=True)
class Report:
    """The verdict, and enough of the evidence to act on it."""

    reach: Reach
    floor: Floor
    note: str = ""

    @property
    def reach_ok(self) -> bool:
        # An unmeasured reach cannot fail: no backend is an absence, not a failure. A MEASURED zero
        # is different -- that is a broken tracer, and it fails.
        return (not self.reach.measured) or self.reach.fraction >= MIN_REACH

    @property
    def floor_ok(self) -> bool:
        return self.floor.fraction <= MAX_FLOOR

    @property
    def ok(self) -> bool:
        return self.reach_ok and self.floor_ok

    def to_json(self) -> dict:
        return {"reach": self.reach.to_json(), "floor": self.floor.to_json(),
                "reach_ok": self.reach_ok, "floor_ok": self.floor_ok, "ok": self.ok,
                "note": self.note}


def assess(reach: Reach, floor: Floor) -> Report:
    """-> a verdict that says which half failed and what to aim a repair at."""
    if reach.measured and reach.fraction < MIN_REACH:
        dark = ", ".join(reach.dark[:5]) or "(the backend named no region)"
        note = ("the corpus executes %.0f%% of the subject, below the %.0f%% floor; least-reached: %s"
                % (100 * reach.fraction, 100 * MIN_REACH, dark))
    elif floor.fraction > MAX_FLOOR:
        note = ("a submission that does nothing already scores %.0f%% (%s); the corpus grades "
                "mostly constants" % (100 * floor.fraction, floor.worst))
    elif not reach.measured:
        note = ("reaches the subject; line coverage was not measured (no backend for this language) "
                "and the floor is %.0f%%" % (100 * floor.fraction))
    else:
        note = ("reaches %.0f%% of the subject; a do-nothing submission scores %.0f%%"
                % (100 * reach.fraction, 100 * floor.fraction))
    return Report(reach, floor, note)
