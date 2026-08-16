"""The pieces, composed: sample probes, freeze a real subject, grade real candidates.

Every module is tested alone elsewhere. This asks the different question -- whether they fit -- and
it is the one that catches an interface that is individually reasonable at both ends and wrong in
the middle. The subjects are files launched as programs, so what is exercised is the whole path a
real task takes.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import scoring                                          # noqa: E402
from frf.observe.call.observation import Observation, freeze, grade    # noqa: E402
from frf.observe.call.runner import Subject                           # noqa: E402
from frf.observe.probes.schema import Schema, sample                  # noqa: E402

# A subject template. `%s` is spliced into the body so one string can produce a reference and a
# candidate that differs from it in a controlled way.
_SUBJECT = '''
import json, sys, time
def run(args):
    xs = args[0]
    return sum(xs) %s
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    rid = req["id"]
    try:
        if req.get("op") == "time":
            started = time.perf_counter()
            for _ in range(int(req.get("repeats", 1))):
                run(req["args"])
            out = {"id": rid, "ok": True, "seconds": time.perf_counter() - started}
        else:
            out = {"id": rid, "ok": True, "value": run(req["args"])}
    except Exception as exc:
        out = {"id": rid, "ok": False, "error": "%%s: %%s" %% (type(exc).__name__, exc)}
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
'''


def _subject(tmp: str, name: str, mutation: str = "") -> str:
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(_SUBJECT % mutation)
    return path


def test_freeze_then_grade_a_correct_and_a_wrong_candidate():
    """The whole path: declare inputs, freeze the reference, grade two submissions.

    The correct candidate must score full correctness and the wrong one zero -- and those are the
    two halves that have to hold together. A verifier that fails only one of them is useless in a
    way no single-module test can see: one that scores everything zero passes every "bad submission
    is rejected" check ever written.
    """
    tmp = tempfile.mkdtemp(prefix="frf-e2e-")
    try:
        reference = _subject(tmp, "reference.py")
        faithful = _subject(tmp, "faithful.py")
        off_by_one = _subject(tmp, "off_by_one.py", "+ 1")

        schema = Schema.from_json({"params": [{"kind": "float_array", "size": "n"}]})
        probes = {"p%d" % i: sample(schema, i, {"n": 6}) for i in range(20)}

        # FREEZE: the reference runs N times and only what it repeats becomes an Expectation.
        expectations = {}
        with Subject([sys.executable, reference]) as subject:
            for probe_id, args in probes.items():
                expectations[probe_id] = freeze(probe_id,
                                                [subject.call("run", args) for _ in range(5)])

        graded = [e for e in expectations.values() if e.graded()]
        assert len(graded) == len(probes), "a deterministic subject should lose nothing"

        def score(path: str) -> scoring.Reward:
            passed = total = 0
            with Subject([sys.executable, path]) as subject:
                for probe_id, args in probes.items():
                    got, want, _ = grade(expectations[probe_id], subject.call("run", args))
                    passed += got
                    total += want
            return scoring.compute(passed, total, 1.0, 1.0)

        correct = score(faithful)
        assert correct.correct and correct.passed == correct.total == 20, correct
        assert correct.reward == scoring.CORRECT_FLOOR, "correct but not faster earns the floor"

        wrong = score(off_by_one)
        assert not wrong.correct and wrong.passed == 0 and wrong.reward == 0.0, wrong
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_nondeterministic_subject_loses_its_probes_rather_than_freezing_noise():
    """A reference that will not repeat itself must not have its noise recorded as behaviour.

    Freezing it would ship a task that fails correct submissions at random, which is worse than
    shipping nothing: it looks sound and misjudges people.
    """
    tmp = tempfile.mkdtemp(prefix="frf-e2e-flaky-")
    try:
        # `random` without a seed: a different answer every call, by construction.
        flaky = os.path.join(tmp, "flaky.py")
        with open(flaky, "w") as fh:
            fh.write('''
import json, random, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    out = {"id": req["id"], "ok": True, "value": random.random()}
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
''')
        schema = Schema.from_json({"params": [{"kind": "int", "low": 0, "high": 10}]})
        with Subject([sys.executable, flaky]) as subject:
            args = sample(schema, 1, {})
            expectation = freeze("p", [subject.call("run", args) for _ in range(5)])

        assert not expectation.graded(), "noise must not become an Expectation"
        assert "different answers" in expectation.drop_reason, expectation.drop_reason
        # And a dropped probe contributes to neither side of the score, so it cannot quietly
        # deflate a candidate that is in fact correct.
        assert grade(expectation, Observation(True, 0.5)) == (0, 0, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
