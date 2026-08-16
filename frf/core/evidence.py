"""Checking the verifier, not the submission.

A verifier decides whether a submission is right. This decides whether the VERIFIER is right, and
the two are so easily confused that the confusion has shipped broken tasks more than once.

THE PAIR THAT MAKES THE REST MEAN ANYTHING. A verifier that returns zero for everything passes every
check of the form "a bad submission must score zero" -- all of them, forever, while being worthless.
A verifier that returns full marks for everything passes every check of the form "the reference must
score full marks". So the battery must bite from BOTH ends: the reference scores full marks AND
nothing else does. Either one alone is satisfied by a constant.

A CHECK MAY BE SKIPPED ONLY IF ITS FAILURE IS STRUCTURALLY IMPOSSIBLE. Some checks do not apply to
some seams -- "a scored point must be about the subject" cannot fail on a seam where every
observation is by construction the return value of a call. That is a reason. "It seems unlikely
here" is not, and the difference is the whole distance between a battery and a ritual.

A ZERO IS AMBIGUOUS, ALWAYS. "The submission was wrong" and "the harness could not run it" both
produce zero, and a check that cannot tell them apart is a rubber stamp: it passes when the thing it
is checking is completely broken. Every verdict below therefore carries what was observed, not just
whether a number matched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class Outcome(Enum):
    """What a check concluded. INCONCLUSIVE is the one that matters.

    A check that could not establish its point must say so rather than passing. The temptation is
    always to treat "nothing went wrong" as "the check succeeded", which is exactly how a battery
    becomes decoration: the mutation nobody could distinguish from the reference gets recorded as
    evidence that mutations are caught.
    """

    HOLDS = "holds"
    FAILS = "fails"
    INCONCLUSIVE = "inconclusive"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True)
class Verdict:
    check: str
    outcome: Outcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        """INCONCLUSIVE is not ok. A task ships on evidence, and 'we could not tell' is not any."""
        return self.outcome in (Outcome.HOLDS, Outcome.NOT_APPLICABLE)

    def to_json(self) -> dict:
        return {"check": self.check, "outcome": self.outcome.value, "detail": self.detail}


@dataclass
class Battery:
    """The checks a task must survive, and the record of how it did.

    Ordered so the cheapest and most decisive run first: there is no point perturbing channels on a
    task whose reference cannot reproduce its own expectations.
    """

    verdicts: list[Verdict] = field(default_factory=list)

    def record(self, verdict: Verdict) -> Verdict:
        self.verdicts.append(verdict)
        return verdict

    @property
    def ok(self) -> bool:
        return bool(self.verdicts) and all(v.ok for v in self.verdicts)

    def failures(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.ok]

    def to_json(self) -> list:
        return [v.to_json() for v in self.verdicts]


# --------------------------------------------------------------------------------------------
# The checks. Each takes callables rather than a task object, so this module stays ignorant of
# what a task is -- which is what lets one battery serve four scales and two seams.
# --------------------------------------------------------------------------------------------
def ceiling(score_reference: Callable[[], tuple[int, int]]) -> Verdict:
    """E1 -- the reference must score full marks against the expectations it produced.

    Without this, a verifier that rejects everything passes the whole rest of the battery. It is
    also the check that catches an expectation the reference cannot actually reproduce, which is the
    commonest way a task ships broken: nobody could ever have passed it.
    """
    passed, total = score_reference()
    if total <= 0:
        return Verdict("ceiling", Outcome.INCONCLUSIVE,
                       "nothing was graded, so the reference scoring 'full marks' means nothing")
    if passed == total:
        return Verdict("ceiling", Outcome.HOLDS, "the reference scores %d/%d" % (passed, total))
    return Verdict("ceiling", Outcome.FAILS,
                   "the reference scores %d/%d against its own expectations, so no submission "
                   "could do better" % (passed, total))


def floor(score_trivial: Callable[[str], tuple[int, int]], names: list[str]) -> Verdict:
    """E2 -- no trivial submission may score full marks, and the worst case is what counts.

    SEVERAL trivial submissions, not one. Which one scores highest depends on the subject: a program
    whose success is silence is matched by one that prints nothing; a program that always fails is
    matched by one that always fails. Trying a single trivial submission measures luck.
    """
    if not names:
        return Verdict("floor", Outcome.INCONCLUSIVE, "no trivial submission was tried")
    worst = None
    for name in names:
        passed, total = score_trivial(name)
        if total <= 0:
            return Verdict("floor", Outcome.INCONCLUSIVE,
                           "nothing was graded while scoring %r" % name)
        share = passed / total
        if worst is None or share > worst[1]:
            worst = (name, share, passed, total)
    name, share, passed, total = worst
    if passed == total:
        return Verdict("floor", Outcome.FAILS,
                       "the trivial submission %r scores full marks (%d/%d)"
                       % (name, passed, total))
    return Verdict("floor", Outcome.HOLDS,
                   "the best trivial submission (%r) collects %d/%d (%.0f%%)"
                   % (name, passed, total, 100 * share))


def channels_bite(perturb: Callable[[str], tuple[bool, bool]], channels: list[str]) -> Verdict:
    """E3 -- every channel claimed to be graded must actually catch a change to it.

    `perturb(channel)` returns (diverged, caught): whether the perturbed reference PROVABLY produced
    a different observation, and whether the verifier rejected it.

    THE TWO ARE NOT THE SAME QUESTION, and conflating them makes this check a rubber stamp. A
    mutant that scores full marks is ambiguous: either the verifier is blind, or the mutation
    never changed anything graded. Inferring blindness from the score alone cannot separate those,
    so divergence is established by comparing observations directly, and a mutation that did not
    diverge is reported as inconclusive rather than counted as evidence.
    """
    if not channels:
        return Verdict("channels-bite", Outcome.INCONCLUSIVE, "no channel was perturbed")
    blind, inert = [], []
    for channel in channels:
        diverged, caught = perturb(channel)
        if not diverged:
            inert.append(channel)
        elif not caught:
            blind.append(channel)
    if blind:
        return Verdict("channels-bite", Outcome.FAILS,
                       "the verifier accepted a submission that provably differs on: %s"
                       % ", ".join(blind))
    if len(inert) == len(channels):
        return Verdict("channels-bite", Outcome.INCONCLUSIVE,
                       "no perturbation changed any observation, so nothing was demonstrated "
                       "about whether the verifier would notice one")
    detail = "every diverging perturbation was caught"
    if inert:
        detail += " (%d channel(s) could not be made to diverge: %s)" % (len(inert),
                                                                         ", ".join(inert))
    return Verdict("channels-bite", Outcome.HOLDS, detail)


def no_runnable_reference(find_runnable: Callable[[str], list[str]],
                          directories: dict[str, str]) -> Verdict:
    """E4 -- neither the solver's tree nor the verifier's own directory may ship a runnable answer.

    BOTH directories, which is the half usually missed. Checking the reference tree the solver reads
    is the obvious part; the verifier's directory holds a runnable reference and every expectation,
    and whether the submission can read it is decided by one setting in the task description.
    """
    if not directories:
        return Verdict("no-runnable-reference", Outcome.INCONCLUSIVE, "no directory was checked")
    found = {}
    for label, path in directories.items():
        hits = find_runnable(path)
        if hits:
            found[label] = hits[:3]
    if found:
        return Verdict("no-runnable-reference", Outcome.FAILS,
                       "a runnable reference is reachable: %s"
                       % "; ".join("%s -> %s" % (k, v) for k, v in found.items()))
    return Verdict("no-runnable-reference", Outcome.HOLDS,
                   "no runnable reference in %s" % ", ".join(sorted(directories)))


def package_reproduces_itself(drive_shipped: Callable[[], tuple[int, int]]) -> Verdict:
    """E7 -- drive the EMITTED package with the reference it ships.

    Every other check measures the build tree. This one opens what was actually written, which is
    where a whole class of fault lives: a reference built in a scratch directory and never copied,
    an expectation frozen against a path absent from the package. Measured elsewhere, 14 of 78
    emitted packages failed exactly this while having passed everything else.
    """
    passed, total = drive_shipped()
    if total <= 0:
        return Verdict("package-reproduces-itself", Outcome.INCONCLUSIVE,
                       "the shipped verifier graded nothing")
    if passed == total:
        return Verdict("package-reproduces-itself", Outcome.HOLDS,
                       "the shipped reference scores %d/%d inside the package" % (passed, total))
    return Verdict("package-reproduces-itself", Outcome.FAILS,
                   "the package's own reference scores %d/%d against the expectations shipped "
                   "beside it" % (passed, total))


def seed_independent(freeze_under_seed: Callable[[int], str], seeds: list[int]) -> Verdict:
    """E8 -- the expectations must not depend on this run's hash ordering.

    A subject that iterates a hash map produces a stable answer within one process and a different
    one in the next. Freezing that records the dictionary order of the machine that happened to run
    the factory, and every solver then fails for reproducing the program's actual behaviour.
    """
    if len(seeds) < 2:
        return Verdict("seed-independent", Outcome.INCONCLUSIVE,
                       "fewer than two seeds were tried")
    digests = {freeze_under_seed(s) for s in seeds}
    if len(digests) == 1:
        return Verdict("seed-independent", Outcome.HOLDS,
                       "identical expectations under %d hash seeds" % len(seeds))
    return Verdict("seed-independent", Outcome.FAILS,
                   "the expectations differ across hash seeds (%d distinct results from %d seeds), "
                   "so what was frozen is this run's ordering rather than the subject's behaviour"
                   % (len(digests), len(seeds)))
