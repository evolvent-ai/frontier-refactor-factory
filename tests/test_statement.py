"""The statement: the rules are public, the answers are not."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf.core.statement import Facts, render                           # noqa: E402


def _facts(**overrides) -> Facts:
    base = dict(name="thing-rewrite", scale="module", description="A thing that does a thing.",
                source_language="python", probes=8, graded_points=64, freeze_runs=5,
                channels=("value",), timed_workloads=3)
    base.update(overrides)
    return Facts(**base)


def test_every_number_comes_from_the_expectation():
    """A statement that says 224 because someone typed 224 will one day say it of a corpus of 180."""
    text = render(_facts(probes=42, graded_points=168, freeze_runs=5))
    assert "**42 probe(s)**" in text
    assert "**168 graded observation(s)**" in text
    assert "run 5 times per probe" in text


def test_it_states_the_rules_and_withholds_the_behaviour():
    """Withholding the behaviour list is the difference between "reimplement this" and "implement
    this specification" -- working out what a program promises is part of the task."""
    text = render(_facts())
    for public in ("score = 0.5", "Do not call", "offline", "digests"):
        assert public in text, public
    # Nothing resembling an enumerated contract, and no expected output.
    assert "behaviour list" not in text.lower()
    assert "expected output" not in text.lower()


def test_partial_correctness_is_advertised_so_a_near_miss_is_repaired():
    """Told only "match exactly", a solver abandons a submission missing 3 of 1567. That one is
    worth repairing, and the score says so."""
    text = render(_facts())
    assert "0.5 x (fraction matched)" in text
    assert "worth repairing rather than abandoning" in text


def test_no_threshold_and_no_ceiling_are_stated_explicitly():
    text = render(_facts())
    assert "no threshold to clear" in text and "no ceiling" in text
    assert "noise" in text, "a difference inside the noise counts as no change, and that is promised"


def test_a_task_with_no_timed_workload_says_so_rather_than_implying_one():
    text = render(_facts(timed_workloads=0))
    assert "scored on behaviour alone" in text
    assert "no speed measurement is taken" in text
    assert "workload(s) held out" not in text, "it must not promise a timing that will not happen"


def test_a_cross_language_task_names_the_enforcement_not_just_the_rule():
    """"Do not rebuild it in the original language" is enforced by the image, and saying which
    stops a solver wasting a run discovering it."""
    text = render(_facts(target_language="rust", source_language="go"))
    assert "Reimplement this program in **rust**" in text
    assert "ships no go toolchain" in text
    assert "enforced by what is installed" in text


def test_a_same_language_task_asks_for_speed_in_place():
    text = render(_facts(target_language=""))
    assert "Make this program **faster**" in text
    assert "optimise it in place" in text


def test_a_cross_language_spec_asks_for_a_reimplementation():
    """The instruction has to SAY the language, or the task is same-language with a label.

    Both halves are required: `task_form` and `target_language`. Carrying only one leaves
    `is_cross` false and renders the ordinary "make it faster" goal, which is what shipped while
    `specify()` filled in neither -- every task.toml on disk said `cross_language = false`.

    This pins the end of the chain the scales feed: config -> Spec -> instruction.
    """
    from frf.core.scale import Spec, TaskForm
    from frf.core.statement import _fallback_instruction

    cross = _fallback_instruction(Spec(
        name="x", scale="module", language="python", description="d",
        invoke=["serve", "f"], entry="f",
        target_language="javascript", task_form=TaskForm.CROSS_LANGUAGE))
    assert "Reimplement the reference (python) in javascript" in cross, cross
    assert "stay in javascript" in cross, cross

    # The same spec WITHOUT the form is the ordinary optimisation task, not a mislabelled rewrite.
    same = _fallback_instruction(Spec(
        name="x", scale="module", language="python", description="d",
        invoke=["serve", "f"], entry="f"))
    assert "Reimplement" not in same, same
    assert "faster" in same, same
