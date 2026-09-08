"""Measurement claims checked against both standalone evaluator implementations."""
import importlib.util
import sys

import pytest

from frf.core import timing
from frf.observe.call.package import verifier_source
from frf.scales.repo import _verifier_source


def load_verifier(tmp_path, monkeypatch, kind):
    source = (verifier_source(isolated=False)
              if kind == "call" else _verifier_source(isolated=False))
    path = tmp_path / (kind + "_verify.py")
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(kind + "_verify", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", ["call", "process"])
def test_standalone_protocol_matches_factory_and_preserves_slowdowns(tmp_path, monkeypatch, kind):
    verifier = load_verifier(tmp_path, monkeypatch, kind)
    args = (lambda p: 2.0, lambda p: 1.0 if p == "fast" else 4.0,
            lambda shape, i: shape, ["fast", "slow"])
    expected = timing.measure(*args).to_json()
    got = verifier.measure(*args).to_json()
    assert got == expected
    assert got["speedup"] == 0.5
    assert got["shapes"] == {"fast": 2.0, "slow": 0.5}


def test_warmup_and_pair_order_are_measured_but_not_scored():
    events = []
    def cost(side, p):
        events.append((side, p))
        return 2.0 if side == "ref" else 1.0
    result = timing.measure(lambda p: cost("ref", p), lambda p: cost("cand", p),
                            lambda shape, i: i, ["work"], samples=4, warmups=2)
    assert events[:8] == [("ref", 0), ("cand", 0), ("cand", 1), ("ref", 1),
                          ("ref", 2), ("cand", 2), ("cand", 3), ("ref", 3)]
    evidence = result.evidence["work"]
    assert len(evidence["samples"]) == len(evidence["self_samples"]) == 4
    assert evidence["warmup_pairs"] == 2


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_measurement_never_becomes_neutral_speedup(bad):
    result = timing.measure(lambda p: 1.0, lambda p: bad, lambda shape, i: i, ["work"])
    assert not result.usable
    assert result.speedup == 0.0
    assert "timing failed" in result.note


def test_call_evaluator_ignores_self_reported_seconds(tmp_path, monkeypatch):
    verifier = load_verifier(tmp_path, monkeypatch, "call")
    program = tmp_path / "liar.py"
    program.write_text(
        "import sys,json\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if r.get('op')=='time': raise RuntimeError('untrusted timing requested')\n"
        " print(json.dumps({'id':r['id'],'ok':True,'value':sum(r['args']),"
        "'seconds':1e-99}),flush=True)\n")
    monkeypatch.setattr(verifier, "TIMED_REPEATS", 2)
    command = [sys.executable, str(program)]
    # The reference cwd is separate, as it is in a delivered task.
    (tmp_path / "reference").mkdir()
    args = type("Args", (), {"workspace": str(tmp_path)})()
    speedup, _ = verifier.measure_speed({"timed": ["x"], "probes": {"x": [2, 3]}},
                                        command, command, args, str(tmp_path))
    assert verifier.TIMING_REPORT["usable"]
    assert 0 < speedup < 100
    assert "evaluator wall clock" in verifier.TIMING_REPORT["cost_boundary"]


def test_process_timing_rejects_fast_wrong_answer(tmp_path, monkeypatch):
    verifier = load_verifier(tmp_path, monkeypatch, "process")
    scenario = {"probe_id": "x", "steps": [{"argv": ["{PROGRAM}", "input"]}]}
    reference = [sys.executable, "-c", "print('right')"]
    candidate = [sys.executable, "-c", "print('wrong')"]
    actual = verifier.run_scenario(scenario, reference, str(tmp_path), ())[0]
    rules = {}
    for channel, value in actual.items():
        digest, count = verifier.stream_digest(str(value))
        rules[channel] = {"digest": digest, "line_count": count, "graded": True}
    verifier.TIMED_EXPECTATIONS = {"x": [rules]}
    speedup, note = verifier.measure_speed({"x": scenario}, ["x"], candidate, reference,
                                          str(tmp_path), ())
    assert speedup == 0
    assert not verifier.TIMING_REPORT["usable"]
    assert "behavior differs" in note


def test_file_contents_and_empty_directories_are_observable(tmp_path, monkeypatch):
    from frf.observe.process.snapshot import tree_lines
    verifier = load_verifier(tmp_path, monkeypatch, "process")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "result.txt"
    output.write_text("correct")
    before = tree_lines(workspace)
    assert verifier.tree_lines(workspace) == before
    output.write_text("WRONG!!")
    assert tree_lines(workspace) != before
    assert verifier.tree_lines(workspace) == tree_lines(workspace)
    before = tree_lines(workspace)
    (workspace / "empty").mkdir()
    assert tree_lines(workspace) != before
    (workspace / "link").symlink_to("result.txt")
    assert verifier.tree_lines(workspace) == tree_lines(workspace)


def test_standalone_stream_masking_matches_freeze_exactly(tmp_path, monkeypatch):
    from frf.observe.process.observation import Stream
    verifier = load_verifier(tmp_path, monkeypatch, "process")
    text = "first\nunstable\nlast\n"
    digest, count = verifier.stream_digest(text, [1])
    assert count == 3
    assert digest == Stream.of(text).digest(frozenset([1]))
