"""The input vocabulary and the three comparators."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.compare import values                                # noqa: E402
from frf.observe.probes.schema import Schema, SchemaError, sample     # noqa: E402


def test_the_same_seed_draws_the_same_probe():
    """An Expectation is worth freezing only if its probe can be produced again."""
    schema = Schema.from_json({"params": [{"kind": "float_array", "size": "n"},
                                          {"kind": "int", "low": 0, "high": 100}]})
    assert sample(schema, 7, {"n": 5}) == sample(schema, 7, {"n": 5})
    assert sample(schema, 7, {"n": 5}) != sample(schema, 8, {"n": 5})
    # A named size lets one schema be drawn at several sizes, which is what the timing pass needs
    # in order to refuse a candidate that only got fast at one convenient size.
    assert len(sample(schema, 7, {"n": 9})[0]) == 9


def test_everything_drawn_can_cross_the_wire():
    """Whatever is drawn has to survive JSON, because that is how it reaches the subject."""
    schema = Schema.from_json({"params": [
        {"kind": "int"}, {"kind": "float"}, {"kind": "bool"}, {"kind": "string", "size": 6},
        {"kind": "bytes", "size": 4}, {"kind": "int_array", "size": 3},
        {"kind": "float_array", "size": 3}, {"kind": "complex_array", "size": 2},
        {"kind": "list", "size": 2, "element": {"kind": "int"}},
        {"kind": "map", "size": 2, "element": {"kind": "float"}}]})
    drawn = sample(schema, 3, {})
    assert json.loads(json.dumps(drawn)) == drawn


def test_a_float_array_is_drawn_as_floats_even_when_the_dtype_is_wrong():
    """A dtype left blank or wrong would silently truncate the distribution to integers.

    The subject's float-specific branches would then never run, and the corpus would grade a
    program the solver never receives.
    """
    schema = Schema.from_json({"params": [{"kind": "float_array", "dtype": "int64", "size": 8}]})
    assert schema.params[0].dtype == "float64"
    assert all(isinstance(x, float) for x in sample(schema, 1, {})[0])

    # A complex array draws BOTH components: dropping the imaginary part collapses a complex
    # subject onto its real path, and any branch testing for complex input never runs.
    complex_schema = Schema.from_json({"params": [{"kind": "complex_array", "size": 4}]})
    drawn = sample(complex_schema, 1, {})[0]
    assert all(isinstance(pair, list) and len(pair) == 2 for pair in drawn), drawn


def test_a_bad_schema_fails_when_it_is_read_not_when_it_is_drawn():
    """Loudly and early: a typo would otherwise produce a corpus of the wrong shape, and the
    freeze would record it without complaint."""
    for bad in ({"params": []},
                {"params": [{"kind": "nonsense"}]},
                {"params": [{"kind": "list"}]}):            # a compound must say what it contains
        try:
            Schema.from_json(bad)
        except SchemaError:
            pass
        else:
            raise AssertionError("accepted a schema it cannot sample: %r" % bad)


def test_structural_ignores_key_order_but_not_types():
    assert values.structural({"a": 1, "b": [2, 3]}, {"b": [2, 3], "a": 1}).same
    assert not values.structural([1, 2], [2, 1]).same, "order in a sequence is the answer"
    # True == 1 in Python. A subject returning one where the reference returned the other has
    # changed its behaviour, and JSON blurring them must not hide that.
    assert not values.structural(True, 1).same
    assert not values.structural({"a": 1}, {"a": 1, "b": 2}).same


def test_the_envelope_accepts_a_reordered_sum_and_rejects_a_wrong_one():
    """The case the envelope exists for: same answer, different last bits.

    A routine that changes its accumulation order -- vectorised, blocked, parallelised -- produces
    a different low-order digit and is correct. Bit-equality would reject every real optimisation
    and accept only the changes that changed nothing.
    """
    reference_error = 1e-12                       # a well-conditioned reference

    reordered = 0.1 + 0.2 + 0.3
    other_order = 0.3 + 0.2 + 0.1
    assert reordered != other_order, "the premise: these differ in the last bits"
    assert values.envelope(reordered, other_order, reference_error).same

    # A real mistake is still caught: this is far outside anything rounding explains.
    assert not values.envelope(1.0, 1.0001, reference_error).same

    # An ill-conditioned reference is allowed to be loose, because it IS loose -- demanding better
    # of the candidate than the reference achieves is asking it to be more correct than correct.
    assert values.envelope(1.0, 1.0001, 1e-3).same


def test_the_envelope_still_compares_structure_exactly():
    """Numbers are approximate; the shape around them is not."""
    assert not values.envelope([1.0, 2.0], [1.0], 1e-9).same
    assert not values.envelope({"a": 1.0}, {"b": 1.0}, 1e-9).same
    # NaN is not close to anything, including itself -- but two NaNs in the same place are the same
    # behaviour, and a subject that legitimately produces one has to be gradeable.
    assert values.envelope(float("nan"), float("nan"), 1e-9).same
    assert not values.envelope(float("nan"), 1.0, 1e-9).same
    assert values.envelope(float("inf"), float("inf"), 1e-9).same


def test_a_mismatch_says_where_it_was():
    """A path, not just "they differ" -- a solver reading the report has to find the thing."""
    verdict = values.structural({"outer": {"inner": [1, 2, 3]}}, {"outer": {"inner": [1, 9, 3]}})
    assert not verdict.same
    assert "outer.inner[1]" in verdict.detail, verdict.detail


def test_freeze_honours_its_budget_on_the_batched_path_too(monkeypatch):
    """The deadline lived inside the per-probe loop, which the batched path skips.

    `call_many` is how every REMOTE freeze runs, and it `continue`s past the loop holding the
    check -- so the budget was declared and never applied on the one path that matters in
    production. Five runs against a subject that answers slowly could then outlast the stated hour
    with only the outer process wrapper to stop them.
    """
    import time

    from frf.observe.call import stages

    monkeypatch.setenv("FRF_FREEZE_MAX_SECONDS", "0")

    class Batched:
        runs = 0

        def call_many(self, name, args_list):
            Batched.runs += 1
            return {i: object() for i, _ in enumerate(args_list)}

    class Observer:
        def subject(self, spec):
            class Ctx:
                def __enter__(self_inner): return Batched()
                def __exit__(self_inner, *_): return False
            return Ctx()

    class Source:
        count = 2

        def draw(self, n):
            return [[1], [2]][:n]

    corpus = stages.freeze(object(), Observer(), Source(), runs=5)
    assert corpus.usable is False
    assert "freeze timeout" in (corpus.unusable_reason or "")
    assert Batched.runs == 0, "the budget was already spent; no run should have been started"


def test_a_freeze_timeout_is_not_reported_as_an_unstable_reference():
    """Running out of time is not disagreeing with yourself.

    A freeze that hits its budget was filed as `will-not-repeat-itself`, which asserts the reference
    contradicted itself across runs. It did not -- it never finished being asked. A real java
    candidate produced exactly that record, and a reader counting unstable references would have
    counted it.

    Still MATERIAL: a subject too slow to answer inside the budget cannot be graded, which is the
    verdict PROBE_TIMEOUT already makes for the same reason.
    """
    import pytest

    from frf.core import pipeline
    from frf.observe.call.stages import Corpus

    timed_out = Corpus(usable=False, unusable_is_timeout=True, discard_rate=1.0,
                       unusable_reason="freeze timeout after 1800s")
    with pytest.raises(pipeline.Stage) as caught:
        pipeline._check_corpus(timed_out)
    assert caught.value.reason == "too-slow-to-freeze", caught.value.reason
    assert caught.value.fault is pipeline.Fault.MATERIAL

    # An ordinary unstable reference keeps its own, different verdict.
    unstable = Corpus(usable=False, discard_rate=1.0)
    with pytest.raises(pipeline.Stage) as caught:
        pipeline._check_corpus(unstable)
    assert caught.value.reason == "will-not-repeat-itself", caught.value.reason
