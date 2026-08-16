"""The call seam, exercised against real subprocesses.

Every subject here is a program written to a file and launched, never a function this test imports.
That is the point of the seam: if these tests passed by calling Python objects, they would prove
nothing about the language independence that is the whole reason the seam is a wire.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.call import protocol                                  # noqa: E402
from frf.observe.call.observation import Observation, freeze, grade     # noqa: E402
from frf.observe.call.runner import Subject, SubjectFailed              # noqa: E402

# A subject in Python, written as a FILE and run as a program. It never imports the framework -- it
# only reads and writes lines, which is all any language has to do to take part.
_PY_SUBJECT = r'''
import json, sys, time

def total(args):
    if not isinstance(args[0], list):
        raise TypeError("expected a list")
    return sum(args[0])

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    rid, op, args = req["id"], req.get("op", "run"), req.get("args", [])
    try:
        if op == "time":
            started = time.perf_counter()
            for _ in range(int(req.get("repeats", 1))):
                total(args)
            out = {"id": rid, "ok": True, "seconds": time.perf_counter() - started}
        else:
            out = {"id": rid, "ok": True, "value": total(args)}
    except Exception as exc:
        out = {"id": rid, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
'''

# The same contract in a different language, to demonstrate that nothing in the framework knows or
# cares. Skipped when no shell interpreter with the tools is available.
_SH_SUBJECT = r'''#!/bin/sh
while IFS= read -r line; do
  [ -z "$line" ] && continue
  id=$(printf '%s' "$line" | sed 's/.*"id"[ ]*:[ ]*\([0-9-]*\).*/\1/')
  printf '{"id":%s,"ok":true,"value":"shell"}\n' "$id"
done
'''


def _write(tmp: str, name: str, body: str, executable: bool = False) -> str:
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(body)
    if executable:
        os.chmod(path, 0o755)
    return path


def test_a_subject_is_called_over_the_wire_and_answers():
    tmp = tempfile.mkdtemp(prefix="frf-call-")
    try:
        path = _write(tmp, "subject.py", _PY_SUBJECT)
        with Subject([sys.executable, path]) as subject:
            got = subject.call("total", [[1, 2, 3]])
            assert got.ok and got.value == 6, got

            # A REFUSAL IS AN ANSWER. How a subject rejects bad input is part of its behaviour, so
            # it is recorded and compared rather than raised out of the harness.
            refused = subject.call("total", ["not a list"])
            assert not refused.ok and "TypeError" in refused.error, refused
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_seam_does_not_know_what_language_it_is_talking_to():
    """The same framework code drives a subject written in a different language."""
    if not shutil.which("sh") or not shutil.which("sed"):
        return
    tmp = tempfile.mkdtemp(prefix="frf-call-sh-")
    try:
        path = _write(tmp, "subject.sh", _SH_SUBJECT, executable=True)
        with Subject([path]) as subject:
            got = subject.call("anything", [1, 2])
            assert got.ok and got.value == "shell", got
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_subject_that_never_starts_is_a_different_finding_from_a_wrong_answer():
    """Not startable and answering wrongly must not collapse into one verdict.

    A wrong answer is a wrong submission. A subject that never ran is a broken build or environment,
    and telling a repair loop the second is the first sends it to fix something that is not broken.
    """
    try:
        with Subject(["/definitely/not/a/program"]):
            pass
    except SubjectFailed as exc:
        assert "could not start" in str(exc), exc
    else:
        raise AssertionError("a subject that cannot be launched must raise SubjectFailed")

    tmp = tempfile.mkdtemp(prefix="frf-call-die-")
    try:
        path = _write(tmp, "dies.py", "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")
        with Subject([sys.executable, path]) as subject:
            try:
                subject.call("anything", [])
            except SubjectFailed as exc:
                # The far side's stderr has to survive: "it crashed" without the message is a bug
                # report nobody can act on.
                assert "boom" in str(exc), exc
            else:
                raise AssertionError("a subject that exits mid-corpus must raise SubjectFailed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_garbled_reply_is_a_failed_call_rather_than_a_crashed_harness():
    """The far side is someone else's program: it can print noise. That is its behaviour."""
    reply = protocol.Response.decode("this is not json\n")
    assert not reply.ok and "unparseable" in reply.error, reply
    non_object = protocol.Response.decode("[1, 2, 3]\n")
    assert non_object.ok is False, "a non-object reply is not an answer"


def test_an_unstable_probe_is_discarded_whole_and_says_so():
    """This seam cannot mask a position, so it drops the probe and reports why.

    A tree has no line 7. Flattening one to invent a coordinate would make a mask point at a
    different field as soon as key order changed -- hiding whatever moved into the slot.
    """
    stable = [Observation(True, {"a": 1, "b": 2}), Observation(True, {"b": 2, "a": 1})]
    frozen = freeze("p1", stable)
    assert frozen.graded(), "key order alone must not make an observation unstable"

    wobbly = freeze("p2", [Observation(True, 1), Observation(True, 2)])
    assert not wobbly.graded()
    assert "different answers" in wobbly.drop_reason, wobbly.drop_reason

    # An ungraded expectation contributes to neither side of the score.
    assert grade(wobbly, Observation(True, 1)) == (0, 0, "")


def test_grading_names_what_kind_of_difference_it_saw():
    frozen = freeze("p", [Observation(True, 42), Observation(True, 42)])
    assert grade(frozen, Observation(True, 42))[:2] == (1, 1)

    passed, total, why = grade(frozen, Observation(False, error="ValueError: nope"))
    assert (passed, total) == (0, 1)
    assert "refused" in why and "ValueError" in why, why


def test_timing_is_measured_on_the_far_side():
    """A subject self-times, so it is not charged for startup and transport.

    On a subject whose real work is a millisecond, this module's own overhead would otherwise BE the
    measurement, and the resulting speedup would describe the pipe.
    """
    tmp = tempfile.mkdtemp(prefix="frf-call-time-")
    try:
        path = _write(tmp, "subject.py", _PY_SUBJECT)
        with Subject([sys.executable, path]) as subject:
            few = subject.time("total", [list(range(1000))], repeats=1)
            many = subject.time("total", [list(range(1000))], repeats=50)
            assert few > 0 and many > few, (few, many)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
