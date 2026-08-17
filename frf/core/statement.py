"""The document the solver reads.

    THE RULES ARE PUBLIC. THE ANSWERS ARE NOT.

Everything about how a submission will be judged is stated: how many probes, which channels are
compared, that correctness must be complete before speed counts at all, that the reference may not
be called. None of what the reference actually does is stated.

WHAT IS DELIBERATELY WITHHELD, and it is the decision most likely to be questioned: the list of
behaviours the program promises. Adequacy computes that list -- it is how the corpus is audited --
and shipping it here would be easy. It stays internal because working out what a program promises is
part of what the task measures. Handing it over converts "reimplement this" into "implement this
specification", which is a different and much smaller exercise.

EVERY NUMBER COMES FROM THE FROZEN EXPECTATION, never from a template constant. A statement that
says "224 observations" because someone typed 224 will one day say it about a corpus of 180. Reading
them from the expectation makes the statement unable to claim something the key cannot back, which
is a property rather than a habit.

WHY THE SCORE IS EXPLAINED RATHER THAN SUMMARISED. Telling a solver only "you must match exactly"
invites abandoning a submission that misses three cases of 1567, when the correct next step is to
repair it -- partial correctness is scored, and it is worth saying so.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Facts:
    """What the statement is allowed to assert, all of it measured rather than declared."""

    name: str
    scale: str
    description: str
    source_language: str
    target_language: str = ""
    probes: int = 0
    graded_points: int = 0
    freeze_runs: int = 0
    channels: tuple = ()
    timed_workloads: int = 0
    forbidden: tuple = ()

    @property
    def cross_language(self) -> bool:
        return bool(self.target_language) and self.target_language != self.source_language


def _task_line(facts: Facts) -> str:
    if facts.cross_language:
        return ("Reimplement this program in **%s**.\n\n"
                "The reference implementation (%s) is in your workspace. Port its behaviour; do not "
                "call it. This image ships no %s toolchain, so the reference cannot be rebuilt here "
                "-- the requirement is enforced by what is installed, not by inspecting what you "
                "submit." % (facts.target_language, facts.source_language, facts.source_language))
    return ("Make this program **faster** without changing what it does.\n\n"
            "The reference is in your workspace. Start from it and optimise it in place. You may "
            "change anything, in any way, as long as the observable behaviour stays identical.")


def _grading(facts: Facts) -> str:
    channels = "\n".join("* **%s**" % name.replace("_", " ") for name in facts.channels)
    return (
        "We run **%d probe(s)** against your submission and compare what comes back with what the "
        "reference produced. For each we look at:\n\n%s\n\n"
        "That is **%d graded observation(s)** in total.\n\n"
        "These expectations were not written by hand. The reference was run %d times per probe and "
        "only what it reproduced *every single time* is graded -- anything it could not repeat, such "
        "as a timestamp or a temporary path, is excluded. You are never asked to reproduce something "
        "that is not reproducible."
        % (facts.probes, channels, facts.graded_points, facts.freeze_runs))


def _scoring(facts: Facts) -> str:
    speed = (
        "Once every graded observation matches, and only then, your submission is timed against the "
        "reference on %d workload(s) held out from grading. Your score rises with the measured "
        "speedup; there is no threshold to clear and no ceiling above which more speed stops "
        "counting. A difference too small to distinguish from the machine's own noise counts as no "
        "change." % facts.timed_workloads
        if facts.timed_workloads else
        "This task is scored on behaviour alone: no workload in it is heavy enough to time honestly, "
        "so no speed measurement is taken.")

    return (
        "```\n"
        "score = 0.5 + 0.5 x speedup      if every observation matches\n"
        "score = 0.5 x (fraction matched)  otherwise\n"
        "```\n\n"
        "Correctness **unlocks** speed rather than being averaged with it: short of complete, no "
        "amount of speed helps. Partial correctness is still scored, so a submission that misses a "
        "few observations is worth repairing rather than abandoning.\n\n" + speed)


def _rules(facts: Facts) -> str:
    forbidden = ("\n".join("* `%s`" % item for item in facts.forbidden)
                 if facts.forbidden else "* the reference implementation, by any route")
    return (
        "* Do not call, import, link against, or shell out to the reference implementation. Your "
        "submission is inspected for this before it is run, and a match scores zero without being "
        "graded or timed.\n"
        "* Do not attempt to read the expectations. They are stored as digests, in an environment "
        "your submission cannot reach.\n"
        "* Everything runs offline.\n\n"
        "Specifically forbidden:\n\n%s" % forbidden)


def render(facts: Facts) -> str:
    """-> the full statement, in the order a solver needs it.

    Task first, because it decides whether to read on. Then what the program is, then how it will be
    judged, then the rules. Nothing here is generated by a model: a model wrote `description`, and
    every other sentence is either fixed or read from the expectation.
    """
    return "\n\n".join((
        "# %s" % facts.name,
        _task_line(facts),
        "## What this program is\n\n%s" % facts.description.strip(),
        "## What you submit\n\nA `/app/run.sh` that runs your program. We invoke it exactly as we "
        "invoke the reference. It must work with no network access.",
        "## How you are graded\n\n%s" % _grading(facts),
        "## Scoring\n\n%s" % _scoring(facts),
        "## Rules\n\n%s" % _rules(facts),
    )) + "\n"
