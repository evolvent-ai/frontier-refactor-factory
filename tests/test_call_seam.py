"""The call seam, exercised against real subprocesses.

Every subject here is a program written to a file and launched, never a function this test imports.
That is the point of the seam: if these tests passed by calling Python objects, they would prove
nothing about the language independence that is the whole reason the seam is a wire.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.call import observation, protocol                     # noqa: E402
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


def test_a_corpus_reports_what_instability_cost_it():
    """Discarding unstable probes silently lets a mostly-nondeterministic subject ship as a small
    tidy task. The rate is the number that reveals it, so the freeze returns it rather than the
    caller having to think to ask."""
    stable = [Observation(True, {"n": 1}) for _ in range(5)]
    unstable = [Observation(True, {"n": i}) for i in range(5)]

    report = observation.freeze_corpus({"a": stable, "b": stable, "c": stable, "d": unstable})
    assert report.attempted == 4
    assert len(report.expectations) == 3 and len(report.discarded) == 1
    assert report.discard_rate == 0.25
    assert report.usable, "a quarter lost is survivable"
    assert report.to_json()["reasons"], "the reason travels with the number"


def test_a_subject_that_mostly_will_not_repeat_is_rejected_not_shrunk():
    """Past the threshold, what survives is the probes that agreed by luck.

    Continuing with those would produce a task whose corpus was selected by chance -- which cannot
    distinguish anything, while looking exactly like a small honest task.
    """
    stable = [Observation(True, {"n": 1}) for _ in range(5)]
    unstable = [Observation(True, {"n": i}) for i in range(5)]

    report = observation.freeze_corpus({"a": stable, "b": unstable, "c": unstable, "d": unstable})
    assert report.discard_rate == 0.75
    assert not report.usable, "this is a reject, not a smaller corpus"

    empty = observation.freeze_corpus({"a": unstable})
    assert not empty.usable and not empty.expectations


def test_the_shipped_python_shim_serves_the_wire_the_factory_speaks():
    """The shim is what makes "any language" concrete, so it is tested as a real process.

    Three things have to hold, and the middle one is the one a careless shim gets wrong: a value
    comes back, a REFUSAL comes back as an answer rather than killing the process, and `time` is
    measured on the far side of the pipe.
    """
    import tempfile
    from frf.observe.call import shims

    shim = shims.load("python")
    assert "python" in shims.available()

    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "subject.py"), "w") as fh:
            fh.write("def entry(args):\n"
                     "    a, b = args\n"
                     "    if b == 0:\n"
                     "        raise ValueError('cannot divide by zero')\n"
                     "    return a / b\n")
        with open(os.path.join(work, shim.template), "w") as fh:
            fh.write(shims.source(shim))

        _, command = shim.commands(work)
        with Subject(command, cwd=work) as subject:
            assert subject.call("run", [6, 3]) == Observation(True, 2.0)

            refused = subject.call("run", [1, 0])
            assert not refused.ok and "cannot divide by zero" in refused.error
            assert "/" not in refused.error, "an error must not carry a host path into the key"

            # ...and the process survived the refusal, so later probes still work.
            assert subject.call("run", [9, 3]) == Observation(True, 3.0)
            assert subject.time("run", [6, 3], repeats=100) > 0.0


def test_every_shim_mentions_the_timing_field_the_protocol_sends():
    """The two halves of the wire are written in different languages and cannot be type-checked
    against each other, so the only thing keeping them in step is a test that reads both.

    THIS CAUGHT A REAL BUG. The encoder sent `repeats` and the Python shim read `n`, so every
    timing request ran the subject exactly once no matter what was asked. Nothing failed: the
    subject answered, the seconds were real, and the mechanism that exists to lift a workload above
    the clock's resolution had simply never operated.

    ONLY `repeats` IS CHECKED TEXTUALLY, and the restraint is the lesson. The first version of this
    test looked for every field as a quoted JSON key and failed on two shims that were correct --
    the C one builds replies as text so the name sits inside a longer literal, and the JavaScript
    one uses unquoted object keys because that is what the language does. A test that has to be
    taught each language's punctuation is testing punctuation. What survives here is the one field
    whose name is arbitrary and shared, which is exactly the one that drifted; everything else is
    checked by running the shim.
    """
    from frf.observe.call import shims
    from frf.observe.call.protocol import Request

    sent = json.loads(Request(1, "entry", [1, 2], op="time", repeats=7).encode())
    assert set(sent) == {"id", "op", "call", "args", "repeats"}, sent

    for language in shims.available():
        source = shims.source(shims.load(language))
        assert "repeats" in source, (
            "the %s shim never mentions `repeats`, so a timing request would run the subject once "
            "however many were asked for -- which is what the Python shim did for its first month"
            % language)


def test_a_timing_request_really_runs_the_subject_that_many_times():
    """The other half of the contract above: the field is read AND acted on.

    A shim could mention `repeats` and ignore it. What makes this checkable without a stopwatch is
    a subject that counts its own calls and can be asked afterwards -- so the assertion is about a
    number the subject reports, not about elapsed time, which would be a flaky way to ask.
    """
    import tempfile
    from frf.observe.call import shims

    shim = shims.load("python")
    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "subject.py"), "w") as fh:
            fh.write("CALLS = [0]\n"
                     "def entry(args):\n"
                     "    CALLS[0] += 1\n"
                     "    return CALLS[0] if args == ['count'] else args\n")
        with open(os.path.join(work, shim.template), "w") as fh:
            fh.write(shims.source(shim))
        _, command = shim.commands(work)

        with Subject(command, cwd=work) as subject:
            subject.time("run", [1], repeats=25)
            counted = subject.call("run", ["count"])
            # 25 timed calls plus this one. Had the shim ignored `repeats`, this would read 2.
            assert counted.value == 26, counted


def test_an_unsupported_language_is_refused_by_name():
    """Falling back to a default would emit a task that fails later with a syntax error."""
    from frf.observe.call import shims

    try:
        shims.load("cobol")
    except LookupError as exc:
        assert "cobol" in str(exc) and "core/" in str(exc)
    else:
        raise AssertionError("an unsupported language must raise rather than guess")
