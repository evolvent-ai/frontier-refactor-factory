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

import re
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
    if facts.channels == ("the value the call returned",):
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


def _strip_preamble(raw: str, name: str) -> str:
    """A model's reply -> the document, without whatever it said before starting to write.

    REASONING MODELS NARRATE, and the system prompt asking for "no preamble" does not stop it. Three
    of four sampled candidates opened with a line like "I'm locating the parser entry point..." and
    in two of them the narration ran straight into the heading with no newline between, so the
    document began `...Go layout in /app.# gofeed-rss-atom-opt`. Nothing downstream caught it:
    `_validate_and_repair` asks only whether each section is PRESENT, and a leaked sentence sitting
    above `# name` leaves every section present. The task then ships with the model's inner monologue
    as its first paragraph, which is exactly the tell that separates generated corpora from
    hand-written ones.

    The document is defined to start at its `# ` heading, so that is where this cuts. The heading is
    matched at a line start or fused to the end of prose; the task's own name is preferred when it
    appears, since a Workspace bullet could otherwise mention a `#` and win. A reply with no heading
    at all is returned unchanged -- it is malformed in some other way, and `_validate_and_repair`
    should be the one to say so rather than this function silently emptying it.
    """
    text = (raw or "").strip()
    if not text:
        return text
    for pattern in (r"(?m)^#\s+%s\s*$" % re.escape(name), r"#\s*%s\b" % re.escape(name),
                    r"(?m)^#\s+\S", r"#\s+\S"):
        found = re.search(pattern, text)
        if found:
            return text[found.start():].lstrip()
    return text


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
    from . import model as _model
    if not _model.available():
        return _fallback_instruction(spec)
    # Two attempts before the static document, matching generate_task_name: a gateway that timed
    # out once frequently answers the retry, and a template instruction is a real loss of quality
    # -- it describes no workspace, no build command and no task-specific constraint.
    for attempt in (0, 1):
        try:
            return _generate_via_model(spec)
        except Exception:                               # noqa: BLE001 -- model errors are expected
            if attempt == 0:
                continue
            return _fallback_instruction(spec)
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

IMPORTANT CONTEXT ABOUT VERIFICATION:
- The harness grades the submission automatically by running the frozen test suite — the agent \
does NOT need to run the verifier manually.
- In the ## Build & Test section, provide: (1) the build command to compile/prepare the project, \
and (2) how the agent can do a quick self-check using the project's own test suite or a sample run \
— this is for the agent's own confidence, not the official grading.
- Do NOT say "run the verifier" or reference test.sh — the harness does that automatically.

Write a markdown task instruction with EXACTLY these sections, in this order:

# {name}

(One sentence: the goal of this task.)

## Workspace

(Bullet list of key files and directories in /app the solver will find. Be specific — name actual \
source directories, entry points, and config files relevant to this task. Do not list generic \
directories like "tests/" since those are off-limits.)

## Build & Test

(Two fenced code blocks: first the build command, then a quick self-check command the agent can \
use to verify their change works — e.g. a unit test run, a sample invocation, or a benchmark. \
Add one sentence before each block explaining what it does.)

## Constraints

**What you CAN do**

(Bullet list, specific to this task's language and form. Include useful techniques specific to \
this language/domain.)

**What you CANNOT do**

(Bullet list, specific to this task. Start with the most important constraint for this particular \
task, then general rules.)

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

    # The document starts at its heading, not at the model's account of how it got there.
    raw = _strip_preamble(raw, spec.name)
    if not raw.strip():
        return _fallback_instruction(spec)

    # Append fixed sections that must not be model-written.
    text = raw.rstrip() + "\n\n" + _FIXED_SUBMISSION_CONTRACT + "\n\n"
    text += _FIXED_TIME_BUDGET + "\n\n"
    text += _FIXED_BEHAVIORAL_RULES + "\n"

    # Validate structure; add any section the model omitted.
    text = _validate_and_repair(text, spec)
    return text


# ---------------------------------------------------------------------------
# LLM-based task name generation
# ---------------------------------------------------------------------------

# FrontierSWE naming examples used as few-shot calibration.
_NAME_EXAMPLES = """
Examples of high-quality task names (FrontierSWE style):
  cranelift-codegen-opt        (Cranelift compiler backend, codegen optimisation)
  pyright-type-checking-opt    (Pyright type checker, type-checking performance)
  ffmpeg-swscale-to-rust       (FFmpeg libswscale, rewrite in Rust)
  libexpat-to-x86asm           (libexpat XML parser, rewrite in x86 assembly)
  notebook-compression-opt     (Jupyter notebook compressor, compression ratio)
  revideo-rendering-opt        (Revideo, rendering pipeline performance)
  jomini-save-parser-opt       (jomini, Paradox save-file parser performance)
  gofeed-feed-parsing-opt      (gofeed, RSS/Atom/JSON feed parsing performance)
  hucre-spreadsheet-engine-opt (hucre, spreadsheet engine performance)
"""


def _stem_from_identity(identity: str) -> str:
    """Extract a clean kebab-case stem from a material identity string."""
    # identity looks like: github:owner/repo-name@commit or owner/repo-name
    stem = identity.rstrip("/").rsplit("/", 1)[-1].replace(".git", "").lower()
    stem = stem.split("@", 1)[0] or stem
    # Apply the same camel/underscore normalisation _slug uses
    out = []
    for i, ch in enumerate(stem):
        if ch in "_. ":
            out.append("-")
        elif ch.isupper() and i and stem[i - 1].islower():
            out.append("-")
            out.append(ch.lower())
        else:
            out.append(ch.lower())
    joined = "".join(out)
    while "--" in joined:
        joined = joined.replace("--", "-")
    return joined.strip("-")


def _try_generate_name(identity: str, description: str,
                        target_language: str, is_cross: bool) -> str:
    """One attempt at generating a task name via the model. Raises ModelError on failure."""
    from . import model as _model

    if is_cross:
        action_guidance = (
            "The task is a CROSS-LANGUAGE rewrite: the agent must reimplement the project in %s. "
            "Use the suffix '-to-%s' (e.g. 'ffmpeg-swscale-to-rust', 'libexpat-to-x86asm')."
            % (target_language, target_language.lower())
        )
    else:
        action_guidance = (
            "The task is an INPLACE performance optimisation: the agent must make the existing "
            "implementation faster without changing behaviour. Use the suffix '-opt'."
        )

    stem = _stem_from_identity(identity)

    prompt = """\
Generate a short, readable task name for a software performance benchmark.

Project repository stem: {stem}
Project description: {description}

{action_guidance}

{examples}

Rules:
1. Format: <project>-<specific-aspect>-opt  OR  <project>-<component>-to-<lang>
2. All lowercase kebab-case, no underscores, no uppercase.
3. 3–5 hyphen-separated segments total (e.g. "gofeed-feed-parsing-opt" = 4 segments).
4. The specific aspect must come from the description — it names WHAT is being optimised or \
rewritten, not just the project. Do not use generic words like "performance", "speed", \
"implementation", "code", or "project" as the aspect.
5. Drop generic repository-namespace prefixes like "algorithm-practice-", "hacker-rank-", \
"leet-code-", "javascript-algorithms-", "java-algorithms-implementation-", \
"data-structures-and-algorithms-" from the project stem — they describe the repo collection, \
not this task.
6. AVOID REDUNDANCY: if the project stem already contains a word, do not repeat a synonym or \
inflection of it in the aspect. For example: "app-info-parser" + aspect "parsing" → redundant; \
use the thing being parsed instead, e.g. "app-info-parser-apk-ipa-opt". Similarly \
"dbt-extractor" + "extraction" → redundant; use "dbt-extractor-sql-metadata-opt" instead. \
"tr-lang" + "language" → redundant; use "tr-lang-interpreter-opt" instead.
6a. This applies to COMPOUND stems too — read the stem as the words it is built from, even when \
they are not hyphenated. "gofeed" is "go"+"feed", so aspect "feed-parsing" repeats "feed"; write \
"gofeed-rss-atom-opt" instead. "jsode" is "json"+"decode", so "json-parsing" is redundant; name \
the payload or stage instead. "pyclustering" already carries "clustering". When the stem contains \
the domain word, the aspect should name the CONCRETE INPUT, FORMAT, or COMPONENT (e.g. "rss-atom", \
"apk-ipa", "sql-metadata", "save-file", "bytecode") rather than restating the domain.
7. Output ONLY the task name — no explanation, no punctuation, no quotes, no newline.
""".format(
        stem=stem,
        description=description or "(no description)",
        action_guidance=action_guidance,
        examples=_NAME_EXAMPLES,
    )

    raw = _model.ask(prompt, system="Output only the task name, nothing else.", temperature=0.1,
                     timeout=_NAME_TIMEOUT)
    name = raw.strip().lower().strip('"\'`').strip()
    # Basic sanity: must look like kebab-case, no spaces, no capital letters
    if not name or " " in name or name != name.lower() or len(name) < 4 or len(name) > 80:
        raise _model.ModelError("generated name failed sanity check: %r" % name)
    return name


def _kebab(text: str) -> str:
    """Lower kebab-case with camel humps split. Same rule the scales' _slug applies."""
    out = []
    raw = str(text)
    for index, char in enumerate(raw):
        if char in "_. /":
            out.append("-")
            continue
        if char.isupper() and index and (raw[index - 1].islower() or raw[index - 1].isdigit()):
            out.append("-")
        out.append(char.lower())
    joined = "".join(ch for ch in "".join(out) if ch.isalnum() or ch == "-")
    while "--" in joined:
        joined = joined.replace("--", "-")
    return joined.strip("-")


def _apply_suffix(name: str, suffix: str) -> str:
    """Force `name` to end in exactly one `-<suffix>`, dropping any action word the model added."""
    name = name.strip("-")
    for tail in ("-opt", "-optimization", "-optimisation", "-perf", "-faster", "-rewrite"):
        if name.endswith(tail):
            name = name[: -len(tail)]
            break
    if suffix.startswith("to-"):
        # A cross-language model answer may already carry '-to-rust'; strip before re-appending.
        marker = "-" + suffix
        if name.endswith(marker):
            name = name[: -len(marker)]
    return "%s-%s" % (name.strip("-"), suffix)


# One name is not worth a nine-minute stall. The default FRF_LLM_TIMEOUT is 900s and naming runs
# once per CANDIDATE -- including the ones later refused -- so at the default a batch could spend
# half an hour of wall clock on names for tasks that never ship. Gateway timeouts were already the
# largest single our-fault refusal reason measured in this pipeline; a short deadline here keeps
# naming from adding to it, because the deterministic stem name is a perfectly serviceable answer.
_NAME_TIMEOUT = 120.0


def generate_task_name(identity: str, description: str,
                        target_language: str = "", symbol: str = "") -> str:
    """A task name: readable, unique, and ending in the action this task asks for.

    Suffix is uniform across all four scales -- `-opt` for an inplace optimisation, `-to-<lang>`
    for a cross-language rewrite -- so a corpus does not spell the same intent three ways.

    UNIQUENESS IS STRUCTURAL, NOT HOPED FOR. The repository stem always leads the name, because
    hundreds of repositories contain a `sort` or a `parse` and a model asked for a name for one of
    them cannot know about the others. Emitting two `checksum-function-opt` directories would make
    the second overwrite the first, and an audit keyed on (scale, name, language) would count the
    pair once. When `symbol` is given -- kernel and module, where one repository yields many tasks
    -- the symbol is part of the anchor too, and no model is consulted at all: the symbol already
    names what is being optimised, so `g6-measure-text-opt` is both descriptive and derived, and a
    resumed run reproduces it exactly.

    Only repo and package scale consult the model, and only for the middle of the name. Two
    attempts, then the deterministic stem name -- which is also what an empty description gets,
    since a model with nothing to read cannot beat it.

    Args:
        identity: material identity (e.g. 'github:owner/repo@commit')
        description: human-readable description of the project
        target_language: non-empty for cross-language rewrite tasks
        symbol: the function/method under test, for kernel and module scale
    """
    from . import model as _model

    stem = _stem_from_identity(identity)
    is_cross = bool(target_language) and target_language.lower() != "same"
    suffix = ("to-%s" % _kebab(target_language)) if is_cross else "opt"

    if symbol:
        return "%s-%s-%s" % (stem, _kebab(symbol), suffix)

    anchored = "%s-%s" % (stem, suffix)
    if not (description or "").strip() or not _model.available():
        return anchored

    for attempt in (0, 1):
        try:
            name = _try_generate_name(identity, description, target_language, is_cross)
        except Exception:                               # noqa: BLE001
            if attempt == 0:
                continue
            return anchored
        name = _kebab(name)
        if not (name == stem or name.startswith(stem + "-")):
            name = "%s-%s" % (stem, name)
        return _apply_suffix(name, suffix)

    return anchored
