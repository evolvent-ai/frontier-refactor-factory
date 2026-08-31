"""The census of what has been produced, and the ways a census can flatter itself.

Written after a hand count of this project's own output disagreed with the handoff notes twice over.
The notes said 105 tasks in five languages; the directories held 105 task files but only 61 distinct
subjects, because roll mode rebuilds one candidate at a time and a subject re-frozen with a different
probe count leaves another directory behind. Ten "JavaScript tasks" were one JavaScript subject built
ten times.

A yield figure whose numerator counts rebuilds is not a yield figure, and a matrix that cannot tell an
unexamined task from an audited one is what let two consecutive sessions re-derive the same conclusion
by hand. So the tests below are about counting the right unit and refusing to overstate.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import attestation, audit                                  # noqa: E402


_TOML = """\
schema_version = "1.4"

[task]
name = "%(scale)s/%(name)s"

[metadata]
scale = "%(scale)s"
source_language = "%(language)s"
"""


def _task(root, name, *, scale="module", language="python", extra_metadata=""):
    directory = os.path.join(str(root), name)
    os.makedirs(directory, exist_ok=True)
    body = _TOML % {"scale": scale, "language": language, "name": name}
    with open(os.path.join(directory, "task.toml"), "w") as handle:
        handle.write(body + extra_metadata)
    return directory


def _attest(batch, name, checks, *, backend="remote"):
    attestation.write(str(batch), attestation.build(
        name=name, scale="module", source_language="python", backend=backend, verdicts=checks))


def _held(*names):
    return [{"check": n, "outcome": "holds", "detail": ""} for n in names]


_DECISIVE = list(audit.DECISIVE_CHECKS)


def test_the_unit_is_a_subject_rather_than_a_directory(tmp_path):
    """One subject rebuilt three times is one subject, and the rebuild count stays visible.

    This is the miscount that made the output look three times healthier than it was: the same
    identity appears in several batch directories, each a separate freeze of the same material.
    """
    for batch in ("run-a", "run-b", "run-c"):
        _task(tmp_path / batch, "same-subject")
    _task(tmp_path / "run-a", "other-subject")

    report = audit.report(str(tmp_path))
    assert report["subjects"] == 2, "three copies of one subject are not three subjects"
    assert report["task_directories"] == 4
    rebuilt = next(s for s in audit.walk(str(tmp_path)) if s.identity == "same-subject")
    assert rebuilt.copies == 3


def test_output_nested_by_roll_mode_is_still_found(tmp_path):
    """Roll mode writes to `.candidates/<hash>/`, which is where this project's TypeScript went.

    A flat listing of the batch directories showed nothing for the newest languages while five
    TypeScript subjects sat in hashed subdirectories.
    """
    _task(tmp_path / "batch" / ".candidates" / "9f8e7d6c5b4a", "buried",
          scale="module", language="typescript")
    cells = {(c["scale"], c["language"]): c for c in audit.report(str(tmp_path))["cells"]}
    assert cells[("module", "typescript")]["subjects"] == 1


def test_a_task_with_no_record_is_unrecorded_and_never_a_pass(tmp_path):
    """An absent record is a gap in our bookkeeping, not a statement about the task."""
    _task(tmp_path / "batch", "unexamined")
    subject, = audit.walk(str(tmp_path))
    assert subject.status is audit.UNRECORDED
    assert not subject.attested


def test_a_record_of_only_cheap_checks_is_partial_rather_than_attested(tmp_path):
    """The flaw this distinction exists to close.

    "Every recorded check held" is satisfied by a record holding one offline schema validation, so a
    retroactive audit would parade as fully verified. Attested requires the checks that cannot be
    obtained without running the subject.
    """
    batch = tmp_path / "batch"
    _task(batch, "schema-only")
    _attest(batch, "schema-only", _held("harbor-schema-valid"), backend="")

    subject, = audit.walk(str(tmp_path))
    assert subject.status is audit.PARTIAL, "a cheap check must not read as full attestation"
    assert subject.checks_held == subject.checks_total == 1


def test_a_record_holding_the_decisive_checks_is_attested(tmp_path):
    batch = tmp_path / "batch"
    _task(batch, "properly-checked")
    _attest(batch, "properly-checked", _held(*_DECISIVE, "harbor-schema-valid"))

    subject, = audit.walk(str(tmp_path))
    assert subject.status is audit.ATTESTED
    assert subject.backend == "remote"


def test_a_failed_check_is_reported_as_failing_not_as_a_gap(tmp_path):
    """Failed and unrecorded are different facts and are never merged."""
    batch = tmp_path / "batch"
    _task(batch, "broke")
    _attest(batch, "broke", _held(*_DECISIVE[1:]) + [
        {"check": "ceiling", "outcome": "fails", "detail": "12/57"}])

    subject, = audit.walk(str(tmp_path))
    assert subject.status is audit.ATTESTED_FAILING


def test_an_empty_battery_does_not_read_as_success(tmp_path):
    """A record that checked nothing is not a record of a task that passed."""
    batch = tmp_path / "batch"
    _task(batch, "vacuous")
    _attest(batch, "vacuous", [])
    subject, = audit.walk(str(tmp_path))
    assert subject.status is audit.ATTESTED_FAILING


def test_the_strongest_evidence_among_rebuilds_wins_rather_than_directory_order(tmp_path):
    """A subject attested in one batch is attested; `os.walk` order must not decide the answer."""
    _task(tmp_path / "run-a", "twice-built")
    attested = tmp_path / "run-b"
    _task(attested, "twice-built")
    _attest(attested, "twice-built", _held(*_DECISIVE))

    subject, = audit.walk(str(tmp_path))
    assert subject.status is audit.ATTESTED
    assert subject.copies == 2


def test_a_summary_alone_cannot_claim_full_attestation(tmp_path):
    """A task copied away from its batch keeps its summary, which names no checks.

    Without the names there is no evidence of decisive coverage, so it is partial -- reported as what
    is known rather than promoted on trust.
    """
    _task(tmp_path / "batch", "travelled", extra_metadata=(
        'evidence_schema = "frf-evidence/1"\n'
        'evidence_digest = "sha256:abc"\n'
        'evidence_checks_held = 8\n'
        'evidence_checks_total = 8\n'
        'evidence_backend = "remote"\n'))
    subject, = audit.walk(str(tmp_path))
    assert subject.status is audit.PARTIAL
    assert subject.backend == "remote"


def test_a_registered_language_with_no_output_is_named(tmp_path):
    """An empty cell is invisible in a table built only from what exists, and cannot be certified."""
    _task(tmp_path / "batch", "only-python")
    absent = audit.report(str(tmp_path))["languages_without_output"]
    assert {"java", "ruby", "cpp"} <= set(absent)
    assert "python" not in absent


def test_the_report_is_json_serialisable_and_stable(tmp_path):
    """It is meant to be diffed between runs, so it has to serialise and to order deterministically."""
    _task(tmp_path / "batch", "b-subject", language="go", scale="repo")
    _task(tmp_path / "batch", "a-subject")
    first = audit.report(str(tmp_path))
    json.dumps(first)
    assert [(c["scale"], c["language"]) for c in first["cells"]] == [
        ("module", "python"), ("repo", "go")]
    assert first == audit.report(str(tmp_path))


def test_the_in_image_check_is_decisive_when_it_ran_and_silent_when_it_did_not():
    """A corpus produced before the gate existed carries no such verdict.

    Demanding one would retro-fail every task rather than describe it. Demanding nothing would let a
    task that FAILED the gate stay attested, which is the whole point of adding it.
    """
    from frf.core.audit import _status_from, ATTESTED, PARTIAL

    def record(extra):
        checks = [{"check": "ceiling", "outcome": "holds"},
                  {"check": "floor", "outcome": "holds"},
                  {"check": "package-reproduces-itself", "outcome": "holds"}] + extra
        return {"checks_held": len(checks), "checks_total": len(checks),
                "backend": "remote", "checks": checks}

    assert _status_from({}, record([]))[0] == ATTESTED, "silent when it did not run"
    assert _status_from({}, record(
        [{"check": "reproduces-in-its-own-image", "outcome": "holds"}]))[0] == ATTESTED
    assert _status_from({}, record(
        [{"check": "reproduces-in-its-own-image", "outcome": "fails"}]))[0] != ATTESTED, \
        "a task that failed the gate must not stay attested"
