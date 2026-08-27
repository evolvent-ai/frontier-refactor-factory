"""What was established about a task, and whether it survives being written down.

This exists because of a failure with no symptom. The evidence battery ran on every task the factory
ever emitted, and `emit()` handed its verdicts on as `provenance["evidence"]` -- but the writer only
rendered five provenance keys into a prose sentence, so the verdicts were computed, passed along, and
dropped. Nothing downstream could tell an audited task from an unexamined one, which is why two
consecutive sessions re-derived the state by hand and each concluded "evidence still insufficient".

So the properties worth testing are the ones whose absence hid that: a claim must reach disk, an
absent claim must read as absent rather than as a pass, and neither may disturb the public task
protocol on the way.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import attestation                                         # noqa: E402


def _record(**over):
    base = dict(name="subject-faster", scale="module", source_language="python",
                backend="remote", probes=57, graded_points=57, freeze_runs=5,
                verdicts=[{"check": "ceiling", "outcome": "holds", "detail": "57/57"},
                          {"check": "floor", "outcome": "holds", "detail": "3/57"}])
    base.update(over)
    return attestation.build(**base)


def test_the_backend_is_recorded_because_it_decides_what_the_rest_is_worth():
    """Expectations frozen in this process describe this host, not the task's environment.

    A reader who cannot tell a container run from a local one cannot audit anything, so this field is
    never defaulted to something reassuring -- an unknown backend stays empty.
    """
    assert _record(backend="remote")["backend"] == "remote"
    assert _record(backend="")["backend"] == "", "an unknown backend must not be guessed"


def test_a_number_nobody_recorded_is_absent_rather_than_zero():
    """"Distilled from 0 runs" is a false claim; "nobody recorded it" is the true one.

    The pipeline's corpus contract is four attributes, and freeze_runs is not among them -- a scale
    may legitimately supply a corpus that never counted them. Writing zero there would state
    something about the freeze that no one measured.
    """
    assert "freeze_runs" not in _record(freeze_runs=None)["corpus"]
    assert _record(freeze_runs=5)["corpus"]["freeze_runs"] == 5
    assert "adequacy" not in _record()


def test_held_counts_not_applicable_but_never_inconclusive():
    """INCONCLUSIVE established nothing, so it cannot be counted towards what held.

    NOT_APPLICABLE is different and is counted: a check that cannot fail on this seam for a
    structural reason has been disposed of, which is not the same as one that could not decide.
    """
    record = _record(verdicts=[{"check": "a", "outcome": "holds"},
                               {"check": "b", "outcome": "not-applicable"},
                               {"check": "c", "outcome": "inconclusive"},
                               {"check": "d", "outcome": "fails"}])
    assert (record["checks_held"], record["checks_total"]) == (2, 4)


def test_the_digest_covers_the_verdicts_and_not_the_clock():
    """A summary has to be matchable against its sidecar, across a rewrite that changed nothing.

    Digesting the whole record would change it on every write -- the timestamp moves -- and a digest
    that always differs cannot detect the thing it is for: a task carrying another task's evidence.
    """
    first, second = _record(), _record()
    assert first["recorded_at"] != second["recorded_at"] or True     # clock may not have ticked
    assert attestation.digest(first) == attestation.digest(second)

    moved = _record(verdicts=[{"check": "ceiling", "outcome": "fails", "detail": "12/57"}])
    assert attestation.digest(moved) != attestation.digest(first)


def test_a_summary_carries_only_the_keys_the_task_file_may_gain():
    """What lands in a public task file is a decision made once, not whatever a caller passed."""
    summary = attestation.summary(_record(extra={"secret_field": "should not travel"}))
    assert set(summary) <= set(attestation.SUMMARY_KEYS)
    assert "secret_field" not in summary


def test_a_record_survives_a_round_trip(tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    written = attestation.write(str(batch), _record())
    assert attestation.DIRECTORY in written, "bookkeeping belongs beside the task, not inside it"
    assert attestation.read(written)["checks"] == _record()["checks"]


def test_a_half_written_record_is_never_readable_as_evidence(tmp_path):
    """A crash mid-write must not leave a file that parses as a smaller battery."""
    batch = tmp_path / "batch"
    batch.mkdir()
    path = attestation.write(str(batch), _record())
    leftovers = [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".partial")]
    assert not leftovers, leftovers


def test_an_unreadable_record_is_a_gap_and_never_a_pass(tmp_path):
    """The reader's failure mode has to be "unknown", because the alternative is a false clean bill."""
    assert attestation.read(str(tmp_path / "absent.json")) == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert attestation.read(str(broken)) == {}
    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2, 3]")
    assert attestation.read(str(wrong_shape)) == {}


def test_records_are_found_however_deeply_a_run_nested_them(tmp_path):
    """Roll mode buries each candidate under `.candidates/<hash>/`, so a flat scan finds nothing.

    This is not hypothetical: it is why a census of the existing output counted 83 task files in
    batch directories while the newest languages were sitting unnoticed in hashed subdirectories.
    """
    shallow = tmp_path / "batch"
    deep = shallow / ".candidates" / "abc123def456"
    deep.mkdir(parents=True)
    attestation.write(str(shallow), _record(name="shallow-one"))
    attestation.write(str(deep), _record(name="deep-one"))
    assert {r["task"] for r in attestation.collect(str(tmp_path))} == {"shallow-one", "deep-one"}
