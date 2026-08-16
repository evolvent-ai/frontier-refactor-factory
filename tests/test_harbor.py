"""The shipped format -- one emitter, and the properties a task must have to be safe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import harbor                                           # noqa: E402


def _package(**overrides) -> harbor.Package:
    base = dict(name="example", scale="module", description="A program that does a thing.",
                instruction="Make it faster.", source_language="python",
                provenance={"origin": "https://example.invalid/repo at v1", "probes": 40,
                            "freeze_runs": 5})
    base.update(overrides)
    return harbor.Package(**base)


def test_the_verifier_directory_is_never_readable_by_the_submission():
    """`separate` is not configurable, and this is the check that keeps it that way.

    With a shared environment, tests/ is readable from inside the submission -- and tests/ holds
    every expectation and a runnable reference. Reading the answer key and replaying it would score
    full marks, which makes "do not call the reference" a request rather than a property.
    """
    text = harbor.task_toml(_package())
    assert 'environment_mode = "separate"' in text
    assert "shared" not in text.replace("# ", "", 1) or 'environment_mode = "shared"' not in text
    # And the reason travels with the setting, so nobody flips it back without reading why.
    assert "replaying" in text or "answer key" in text


def test_a_gpu_field_exists_before_any_gpu_task_does():
    """CPU tasks write gpus = 0 rather than omitting the key.

    Emitting it always means the day a GPU task exists, nothing about this file changes -- the field
    the harness reads is already there, with the name the harness already uses.
    """
    cpu = harbor.task_toml(_package())
    assert "gpus = 0" in cpu and "gpu_types = []" in cpu

    gpu = harbor.task_toml(_package(gpus=1, gpu_types=["H100"]))
    assert "gpus = 1" in gpu and '"H100"' in gpu


def test_one_emitter_serves_every_scale():
    """The same function, four scales, no branch. `scale` is data, not a code path."""
    for scale in ("kernel", "module", "package", "repo"):
        text = harbor.task_toml(_package(scale=scale))
        assert 'scale = "%s"' % scale in text
        assert 'name = "%s/example"' % scale in text


def test_cross_language_is_derived_rather_than_declared_twice():
    """One parameter decides which family a task belongs to, so the two cannot disagree."""
    same = _package(source_language="rust", target_language="")
    assert not same.cross_language
    assert "optimisation" in harbor.task_toml(same)

    ported = _package(source_language="rust", target_language="go")
    assert ported.cross_language
    assert "cross-language" in harbor.task_toml(ported)

    # Declaring the same language as a "target" is optimisation, not a port.
    assert not _package(source_language="go", target_language="go").cross_language


def test_the_provenance_sentence_states_what_can_be_checked():
    text = harbor.task_toml(_package())
    assert "example.invalid/repo at v1" in text
    assert "40 probe(s)" in text and "5 repeated runs" in text
    assert "No expected output was hand-authored." in text


def test_the_entry_script_runs_and_writes_the_flat_reward():
    """test.sh is what the harness executes, so it is checked by executing it."""
    tmp = tempfile.mkdtemp(prefix="frf-harbor-")
    try:
        harbor.write(tmp, _package())
        tests = os.path.join(tmp, "tests")

        # A stand-in verifier that writes the detailed report the real one writes.
        with open(os.path.join(tests, "verify.py"), "w") as fh:
            fh.write('''
import json, os, sys
json.dump({"reward": 0.42, "correct": True, "correctness_passed": 7,
           "correctness_total": 7, "speedup": 1.9, "note": "ok"},
          open(os.environ["REWARD_PATH"], "w"))
sys.exit(0)
''')
        logs = os.path.join(tmp, "logs", "verifier")
        os.makedirs(logs, exist_ok=True)

        # Redirect the absolute log path into the sandbox so the script can be run for real here.
        script = open(os.path.join(tests, "test.sh")).read().replace("/logs/verifier",
                                                                     logs)
        run_me = os.path.join(tests, "run_test.sh")
        with open(run_me, "w") as fh:
            fh.write(script)
        os.chmod(run_me, 0o755)

        result = subprocess.run(["bash", run_me], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr

        flat = json.load(open(os.path.join(logs, "reward.json")))
        assert flat["reward"] == 0.42 and flat["correct"] is True
        assert flat["correctness_passed"] == 7 and flat["speedup"] == 1.9
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_verifier_that_writes_nothing_scores_zero_and_says_which_zero():
    """"The verifier failed" and "the submission was wrong" are different findings.

    Both end in a reward of zero, so the note has to distinguish them -- otherwise a broken task
    reads exactly like a bad submission, and nobody goes looking.
    """
    tmp = tempfile.mkdtemp(prefix="frf-harbor-silent-")
    try:
        harbor.write(tmp, _package())
        tests = os.path.join(tmp, "tests")
        with open(os.path.join(tests, "verify.py"), "w") as fh:
            fh.write("import sys\nsys.exit(1)\n")     # writes no report at all
        logs = os.path.join(tmp, "logs", "verifier")
        os.makedirs(logs, exist_ok=True)
        script = open(os.path.join(tests, "test.sh")).read().replace("/logs/verifier", logs)
        run_me = os.path.join(tests, "run_test.sh")
        with open(run_me, "w") as fh:
            fh.write(script)
        os.chmod(run_me, 0o755)

        result = subprocess.run(["bash", run_me], capture_output=True, text=True, timeout=120)
        assert result.returncode != 0, "the verifier's own failure must reach the harness"

        flat = json.load(open(os.path.join(logs, "reward.json")))
        assert flat["reward"] == 0.0
        assert "no report" in flat["note"], flat["note"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
