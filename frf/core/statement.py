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

LLM-GENERATED INSTRUCTIONS. `generate_instruction` builds a structured task instruction using the
model for per-candidate prose (goal, workspace layout, build commands, task-specific constraints),
with fixed sections for rules and scoring that do not vary between tasks. If the model is
unavailable or returns something malformed, every required section is filled from a static template
so the function never returns an empty string or a structurally invalid document.
"""
from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Existing Facts/render interface — kept verbatim so existing tests pass.
# ---------------------------------------------------------------------------

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
    protocol = ""
    if facts.scale in ("module", "kernel", "package"):
        protocol = ("## Interface\n\nThe program is a JSON-lines service. Read one JSON object per line from standard input and "
                    "produce one JSON object per line on standard output. Each request contains an "
                    "`id` and an `args` array; preserve the id in the matching response. A successful "
                    "response has `{\\\"id\\\": id, \\\"ok\\\": true, \\\"value\\\": result}`; a failed "
                    "call has `{\\\"id\\\": id, \\\"ok\\\": false, \\\"error\\\": message}`. The "
                    "reference artifact shows the selected symbol's argument order and result shape.\n")
    return "\n\n".join((
        "# %s" % facts.name,
        _task_line(facts),
        "## What this program is\n\n%s" % facts.description.strip(),
        "## What you submit\n\nA `/app/run.sh` that runs your program. We invoke it exactly as we "
        "invoke the reference. It must work with no network access.",
        protocol.rstrip(),
        "## How you are graded\n\n%s" % _grading(facts),
        "## Scoring\n\n%s" % _scoring(facts),
        "## Rules\n\n%s" % _rules(facts),
    )) + "\n"


# ---------------------------------------------------------------------------
# LLM-based structured instruction generation
# ---------------------------------------------------------------------------

# The sections every instruction must contain, in display order.
_REQUIRED_SECTIONS = (
    "## Workspace",
    "## Build & Test",
    "## Constraints",
    "## Submission Contract",
    "## Time Budget",
    "## Behavioral Rules",
)

_FIXED_SUBMISSION_CONTRACT = """\
## Submission Contract

The harness collects the entire `/app` directory when the agent timeout expires or when your
`run.sh` exits. Scoring runs against whatever is in `/app` at that point.

Correctness is measured first: each graded observation must match the reference's frozen output.
Speed is measured only after every observation matches.

```
score = 0.5 + 0.5 * speedup    if all observations match
score = 0.5 * (fraction matched) otherwise
```

Partial correctness is scored, so a submission that misses a few observations is worth repairing
rather than abandoning."""

_FIXED_TIME_BUDGET = """\
## Time Budget

The harness writes a start timestamp to `/app/.timer/start` when the agent begins. You may read it
to compute elapsed time. The total wall-clock budget is stated in `task.toml`; work that is not
committed to `/app` before the budget expires is not graded."""

_FIXED_BEHAVIORAL_RULES = """\
## Behavioral Rules

* Do not read anything under `tests/`. That directory holds the verifier, the expectations, and a
  runnable reference implementation. Reading it and replaying what you find scores full marks while
  implementing nothing; the directory is in a separate environment that your submission cannot reach.
* Do not call, import, link against, or shell out to the reference implementation. Your submission
  is inspected for this before it is run, and a match scores zero without being graded or timed.
* Everything runs offline. No outbound network access is available."""


def _fallback_instruction(spec) -> str:
    """A fully-valid static instruction when the model call fails or returns garbage.

    Every required section is present, sourced from the spec, so the instruction is never empty
    and never structurally broken.
    """
    from .scale import TaskForm

    is_cross = (spec.task_form is TaskForm.CROSS_LANGUAGE
                and bool(spec.target_language)
                and spec.target_language.lower() != spec.language.lower())

    if is_cross:
        goal = ("Reimplement the reference (%s) in %s so that the observable behaviour is "
                "identical but the implementation is entirely in %s."
                % (spec.language, spec.target_language, spec.target_language))
        can_do = "Rewrite the implementation in %s" % spec.target_language
        cannot_do = ("Call or import the original %s reference; stay in %s"
                     % (spec.language, spec.target_language))
    else:
        goal = ("Make the %s implementation faster without changing its observable behaviour."
                % spec.language)
        can_do = "Change any part of the implementation in %s" % spec.language
        cannot_do = "Change the public API or the language"

    build_cmds = ""
    if spec.build:
        cmds = "\n".join("    " + (" ".join(str(p) for p in cmd)
                                   if isinstance(cmd, list) else str(cmd))
                         for cmd in spec.build)
        build_cmds = "Build commands (run in order inside /app):\n\n%s\n\n" % cmds
    test_cmd = "Run the verifier via the harness (`test.sh`); it reports `reward.json`."

    workspace_items = "* `/app` — your working directory; everything you submit lives here"
    if spec.entry:
        workspace_items += "\n* Entry point / symbol under test: `%s`" % spec.entry

    return "\n\n".join((
        "# %s" % spec.name,
        goal,
        "## Workspace\n\n%s" % workspace_items,
        "## Build & Test\n\n%sTo verify: %s" % (build_cmds, test_cmd),
        "## Constraints\n\n**What you CAN do**\n\n* %s\n\n**What you CANNOT do**\n\n* %s"
        % (can_do, cannot_do),
        _FIXED_SUBMISSION_CONTRACT,
        _FIXED_TIME_BUDGET,
        _FIXED_BEHAVIORAL_RULES,
    )) + "\n"


def _validate_and_repair(text: str, spec) -> str:
    """Ensure every required section is present. Add any missing ones from static templates.

    This structural check is appropriate in the pipeline: a missing section silently deprives the
    solver of information they need to understand the task, which is worse than adding a generic
    fallback for that section.
    """
    for section in _REQUIRED_SECTIONS:
        if section not in text:
            if section == "## Submission Contract":
                text = text.rstrip() + "\n\n" + _FIXED_SUBMISSION_CONTRACT + "\n"
            elif section == "## Time Budget":
                text = text.rstrip() + "\n\n" + _FIXED_TIME_BUDGET + "\n"
            elif section == "## Behavioral Rules":
                text = text.rstrip() + "\n\n" + _FIXED_BEHAVIORAL_RULES + "\n"
            elif section == "## Workspace":
                entry = ("* `/app` — your working directory"
                         + ("\n* Entry point: `%s`" % spec.entry if spec.entry else ""))
                text = text.rstrip() + "\n\n## Workspace\n\n%s\n" % entry
            elif section == "## Build & Test":
                cmds = ""
                if spec.build:
                    cmds = "\n".join("    " + (" ".join(str(p) for p in cmd)
                                               if isinstance(cmd, list) else str(cmd))
                                     for cmd in spec.build)
                    cmds = "Build:\n\n%s\n\n" % cmds
                text = (text.rstrip() + "\n\n## Build & Test\n\n%s"
                        "Verify: run the harness entry point (`test.sh`).\n" % cmds)
            elif section == "## Constraints":
                from .scale import TaskForm
                is_cross = (spec.task_form is TaskForm.CROSS_LANGUAGE
                            and bool(spec.target_language)
                            and spec.target_language.lower() != spec.language.lower())
                if is_cross:
                    can = "Rewrite in %s" % spec.target_language
                    cannot = "Import or call the %s reference" % spec.language
                else:
                    can = "Change any part of the %s implementation" % spec.language
                    cannot = "Change the public API or switch language"
                text = (text.rstrip()
                        + "\n\n## Constraints\n\n**What you CAN do**\n\n* %s\n\n"
                          "**What you CANNOT do**\n\n* %s\n" % (can, cannot))
    return text


def generate_instruction(spec) -> str:
    """Build a structured task instruction for the solver.

    Uses the LLM to write the per-candidate prose (goal, workspace layout, build/test commands,
    task-specific constraints). Fixed sections (Submission Contract, Time Budget, Behavioral Rules)
    are appended from static templates. If the model call fails or returns a structurally incomplete
    document, every missing section is filled from the static templates. Never returns empty string.

    `spec` is a `frf.core.scale.Spec` instance; the function imports `TaskForm` locally so that
    this module does not create a circular dependency at import time.
    """
    from .scale import TaskForm
    try:
        from . import model as _model
        if not _model.available():
            return _fallback_instruction(spec)
        return _generate_via_model(spec)
    except Exception:                                   # noqa: BLE001 -- model errors are expected
        return _fallback_instruction(spec)


def _generate_via_model(spec) -> str:
    """Ask the LLM to write the per-candidate prose sections, then validate and repair."""
    from . import model as _model
    from .scale import TaskForm

    is_cross = (spec.task_form is TaskForm.CROSS_LANGUAGE
                and bool(spec.target_language)
                and spec.target_language.lower() != spec.language.lower())

    if is_cross:
        framing = (
            "Task form: CROSS_LANGUAGE. The solver must reimplement the %s reference in %s. "
            "The goal is behavioural equivalence with better performance through the language "
            "change. Constraints: must stay in %s, must match observable behaviour of the "
            "reference, must deliver a %s implementation the verifier can build."
            % (spec.language, spec.target_language, spec.target_language, spec.target_language))
    else:
        framing = (
            "Task form: INPLACE. The solver must make the existing %s implementation faster "
            "without changing its observable behaviour or public API. Constraints: must stay in "
            "%s, do not change the public API, do not change observable behaviour."
            % (spec.language, spec.language))

    build_info = ""
    if spec.build:
        build_info = "Build commands (as lists, run in /app): %s. " % spec.build

    entry_info = ("Entry point / symbol: %s. " % spec.entry) if spec.entry else ""

    prompt = """\
You are writing the instruction document a software engineer will read before attempting a \
performance-refactoring task. Write in clear, direct technical prose. Do not add preamble or \
sign-off. Output ONLY the markdown document.

Task name: {name}
Scale: {scale}
Language: {language}
Description: {description}
{framing}
{build_info}{entry_info}

Write a markdown task instruction with EXACTLY these sections, in this order:

# {name}

(One sentence: the goal of this task.)

## Workspace

(Bullet list of key files and directories in /app the solver will find. Be specific to this task.)

## Build & Test

(Exact shell commands to build the project and run the verifier. Use fenced code blocks for \
commands.)

## Constraints

**What you CAN do**

(Bullet list, specific to this task and its language/form.)

**What you CANNOT do**

(Bullet list, specific to this task and its language/form.)

Do NOT include ## Submission Contract, ## Time Budget, or ## Behavioral Rules sections — \
those will be appended automatically.
""".format(
        name=spec.name,
        scale=spec.scale,
        language=spec.language,
        description=spec.description or "(no description provided)",
        framing=framing,
        build_info=build_info,
        entry_info=entry_info,
    )

    system = (
        "You write concise, accurate technical task instructions for software engineers. "
        "Output only valid GitHub-flavored markdown. No preamble, no sign-off, no extra sections."
    )

    try:
        raw = _model.ask(prompt, system=system, temperature=0.2)
    except _model.ModelError:
        return _fallback_instruction(spec)

    if not raw or not raw.strip():
        return _fallback_instruction(spec)

    # Append fixed sections that must not be model-written.
    text = raw.rstrip() + "\n\n" + _FIXED_SUBMISSION_CONTRACT + "\n\n"
    text += _FIXED_TIME_BUDGET + "\n\n"
    text += _FIXED_BEHAVIORAL_RULES + "\n"

    # Validate structure; add any section the model omitted.
    text = _validate_and_repair(text, spec)
    return text
