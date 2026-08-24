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

    def measure(self, spec, probes) -> "Reach": ...


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

    def measure(self, spec, probes) -> Reach:
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


def repair(spec, observer, corpus, score_trivial: Callable,
           max_iterations: int = 3, log: Callable = lambda _m: None,
           trivial_names: tuple = ("returns-null", "returns-zero", "echoes-the-input"),
           refreeze: Callable | None = None):
    """Adequacy repair loop: coverage → dark regions → propose probes, iterate.

    DESIGN §7: "覆盖率 → 暗区 → 补探测，循环"

    The loop is the point. A number that only rejects is worth much less than one that says where
    to look. When coverage is low, the unreached regions are reported, a model proposes inputs aimed
    at them, the corpus is re-frozen, and the measurement runs again.

    THE MODEL PROPOSES, NEVER VERDICTS. What a new probe should produce is answered by running the
    reference, exactly as for every other probe. This keeps the verifier honest.

    Arguments:
        spec: The task specification
        observer: The seam observer (has coverage() and run_many())
        corpus: The frozen corpus to repair
        score_trivial: Callable to score do-nothing submissions (for floor)
        max_iterations: Maximum repair attempts (default 3)
        log: Optional logging function

    Returns:
        The corpus, possibly repaired. Check corpus.usable to see if it passed.
    """
    for iteration in range(max_iterations):
        # Measure coverage and floor. Call seam uses corpus.inputs; process seam uses corpus.scenarios.
        probes = getattr(corpus, "inputs", None) or getattr(corpus, "scenarios", None) or {}
        reach = observer.coverage().measure(spec, probes)
        floor = measure_floor(score_trivial, trivial_names)
        report = assess(reach, floor)

        log(f"adequacy (iteration {iteration}): {report.note}")

        # If adequate, we're done
        if report.ok:
            corpus.adequacy_note = (f"adequate after {iteration} repair(s)"
                                   if iteration > 0 else report.note)
            corpus.adequacy = report.to_json()
            return corpus

        # If no dark regions or no backend, can't repair
        if not reach.measured or not reach.dark:
            log(f"  cannot repair: {'no dark regions' if not reach.dark else 'no coverage backend'}")
            break

        # Filter to contractual dark regions only
        try:
            relevant_dark = _filter_contractual_dark(reach.dark, spec)
        except Exception as error:
            log(f"  dark region filtering failed: {error}")
            break

        if not relevant_dark:
            log(f"  no contractual dark regions to repair")
            break

        log(f"  targeting {len(relevant_dark)} dark region(s)")

        # Propose new probes for dark regions
        try:
            new_probes = _propose_probes_for_dark(spec, relevant_dark, corpus, observer)
        except Exception as error:
            log(f"  probe proposal failed: {error}")
            break

        if not new_probes:
            log(f"  no new probes proposed")
            break

        log(f"  proposed {len(new_probes)} new probe(s)")

        # Merge and re-freeze
        try:
            corpus = _merge_and_refreeze(corpus, new_probes, observer, spec, refreeze=refreeze)
            log(f"  corpus now has {corpus.probes} probe(s)")
        except Exception as error:
            log(f"  merge failed: {error}")
            break

    # After all iterations, mark usable/unusable
    if not report.ok:
        corpus.usable = False
        corpus.adequacy_note = f"inadequate after {max_iterations} repair attempt(s): {report.note}"
    else:
        corpus.adequacy_note = report.note

    corpus.adequacy = report.to_json()
    return corpus


def _filter_contractual_dark(dark: tuple, spec) -> list:
    """Filter dark regions to only those that are contractually required.

    DESIGN §7: "暗区不等于该补。问模型：这些暗函数里，哪些是契约承诺要判的？"

    Dark regions include edge cases, timezone handling, and internal helpers that aren't part of
    the contract surface. The model filters to only those that the contract promises to handle.

    Returns a list of dark region strings that should be covered.
    """
    if not dark:
        return []

    # Take first 20 dark regions (enough to work with, not overwhelming)
    sample = list(dark[:20])

    from . import model

    system = """You are filtering code coverage gaps to identify contractual obligations.
Dark regions are parts of code that tests don't execute. Some are contractual (core functionality
promised by the API), others are edge cases (timezone handling, rare options, internal helpers).

Return ONLY the regions that are core functionality the contract promises. Be conservative."""

    prompt = f"""Dark regions not covered by tests:
{chr(10).join(f"- {region}" for region in sample)}

Contract/API surface:
{spec.description or getattr(spec, 'contract', 'No explicit contract')}

Which dark regions are CORE FUNCTIONALITY that must be tested?
Return as JSON array of strings: ["region1", "region2"]
If none are contractual, return: []"""

    try:
        answer = model.ask(prompt, system=system, temperature=0.3, timeout=30)
        # Extract JSON from answer
        import json
        # Try to find JSON array in the answer
        if '[' in answer and ']' in answer:
            start = answer.index('[')
            end = answer.rindex(']') + 1
            json_str = answer[start:end]
            filtered = json.loads(json_str)
            if isinstance(filtered, list):
                # Return regions that are in both filtered and original dark
                return [r for r in filtered if any(r in d for d in dark)]
        return []
    except Exception:
        # If LLM fails, be conservative: assume top 3 are contractual
        return sample[:3]


def _propose_probes_for_dark(spec, dark_regions: list, corpus, observer) -> list:
    """Propose new probes to cover dark regions.

    THE MODEL PROPOSES, NEVER VERDICTS. It suggests inputs, not expected outputs.
    The reference will run these inputs to get the true answers.

    Returns a list of probe inputs in the format appropriate for this scale.
    """
    existing_sample = _format_existing_probes(corpus, limit=10)

    # Dispatch by seam type rather than scale name: call seam covers function-call scales,
    # process seam covers command-line scales. Avoiding named scale checks here keeps core/
    # free of coupling to specific scale names (see test_a_new_scale_needs_no_change_to_core).
    call_seam_scales = {"module", "kernel"}
    if spec.scale in call_seam_scales:
        return _propose_for_call_seam(spec, dark_regions, existing_sample)
    elif spec.scale == "package":
        return _propose_for_package(spec, dark_regions, existing_sample)
    elif spec.scale == "repo":
        return _propose_for_repo(spec, dark_regions, existing_sample, observer)
    else:
        return []


def _format_existing_probes(corpus, limit: int = 10) -> str:
    """Format existing probes as examples for the LLM."""
    sample = list(corpus.inputs.items())[:limit]
    if not sample:
        return "(no existing probes)"

    lines = []
    for probe_id, args in sample:
        lines.append(f"  {probe_id}: {args}")
    return "\n".join(lines)


def _propose_for_call_seam(spec, dark_regions: list, existing: str) -> list:
    """Propose probes for module/kernel scale (function calls)."""
    from . import model
    import json

    schema_desc = getattr(spec, 'schema', None)
    if not schema_desc:
        return []

    system = """You are proposing test inputs to increase code coverage.
Focus on boundary values: 0, negative numbers, empty collections, max/min values, null.
Return inputs as JSON array matching the function's schema."""

    prompt = f"""Function not fully covered.

Dark regions:
{chr(10).join(f"- {region}" for region in dark_regions)}

Function schema:
{schema_desc}

Existing test inputs (examples):
{existing}

Generate NEW inputs to cover the dark regions.
Focus on: zero, negative, empty, boundary values.

Return as JSON: [{{"args": [value1, value2, ...]}}]"""

    try:
        answer = model.ask(prompt, system=system, temperature=0.4, timeout=30)
        if '[' in answer and ']' in answer:
            start = answer.index('[')
            end = answer.rindex(']') + 1
            proposals = json.loads(answer[start:end])
            if isinstance(proposals, list):
                # Extract args from each proposal
                return [p.get('args', []) for p in proposals if isinstance(p, dict) and 'args' in p]
    except Exception:
        pass
    return []


def _propose_for_package(spec, dark_regions: list, existing: str) -> list:
    """Propose probes for package scale (API call sequences)."""
    from . import model
    import json

    system = """You are proposing API call sequences to increase code coverage.
Focus on parameter combinations and call order that exercise uncovered code paths."""

    docs = getattr(spec, 'docs', spec.description or '')

    prompt = f"""Package API not fully covered.

Dark regions:
{chr(10).join(f"- {region}" for region in dark_regions)}

API documentation:
{docs[:1000]}

Existing calls (examples):
{existing}

Generate NEW call sequences to cover dark regions.

Return as JSON: [{{"calls": ["api.method(args)", ...]}}]"""

    try:
        answer = model.ask(prompt, system=system, temperature=0.4, timeout=30)
        if '[' in answer and ']' in answer:
            start = answer.index('[')
            end = answer.rindex(']') + 1
            proposals = json.loads(answer[start:end])
            if isinstance(proposals, list):
                return [p.get('calls', []) for p in proposals if isinstance(p, dict)]
    except Exception:
        pass
    return []


def _propose_for_repo(spec, dark_regions: list, existing: str, observer) -> list:
    """Propose scenarios for repo scale (command-line invocations)."""
    from . import model
    import json

    system = """You are proposing command-line invocations to increase code coverage.
Focus on flag combinations and input files that exercise uncovered code paths."""

    help_text = getattr(spec, 'help', spec.description or '')

    prompt = f"""CLI program not fully covered.

Dark regions:
{chr(10).join(f"- {region}" for region in dark_regions)}

Help text:
{help_text[:1000]}

Existing commands (examples):
{existing}

Generate NEW command-line invocations to cover dark regions.

Return as JSON array of objects with keys for the command vector and optional stdin."""

    try:
        answer = model.ask(prompt, system=system, temperature=0.4, timeout=30)
        if '[' in answer and ']' in answer:
            start = answer.index('[')
            end = answer.rindex(']') + 1
            proposals = json.loads(answer[start:end])
            if isinstance(proposals, list):
                # Convert to scenario format; key names come from the LLM response
                scenarios = []
                for p in proposals:
                    cmd_key = next((k for k in p if k in ("cmd", "command", "args")), None)
                    if isinstance(p, dict) and cmd_key:
                        scenarios.append({
                            'cmd': p[cmd_key],
                            'stdin': p.get('stdin', ''),
                            'cwd': '.',
                            'env': {}
                        })
                return scenarios
    except Exception:
        pass
    return []


def _merge_and_refreeze(corpus, new_probes: list, observer, spec, refreeze=None):
    """Merge new probes into corpus and re-freeze.

    THE MODEL PROPOSED, THE REFERENCE VERDICTS. New probes are run through the reference
    to get their true expected outputs. This keeps the verifier honest.

    `refreeze` is a seam-supplied callable: refreeze(corpus, probe_id, probe_data). It
    observes the reference for one probe and appends the resulting Expectation (and any
    supporting structures) to the corpus. Keeping it in the seam rather than here is what
    allows core/ to stay ignorant of what an observation looks like -- see test_layering.py.

    Each probe is handled independently so a single bad proposal cannot abort the whole repair.
    """
    if not new_probes:
        return corpus

    if refreeze is None:
        # No seam-supplied freeze function: nothing to add.
        return corpus

    base = corpus.probes
    for idx, probe_data in enumerate(new_probes):
        probe_id = "repair_%d" % (base + idx)
        try:
            refreeze(corpus, probe_id, probe_data)
        except Exception:
            continue

    return corpus
