"""Control queries must never substitute for successful program workloads."""
import hashlib
import sys
from types import SimpleNamespace

import pytest

from frf.observe.process import stages
from frf.observe.process.runner import Scenario, Step, run_scenario
from frf.observe.process.workload import eligible_timing_steps, timing_steps
from tests.test_timing_protocol import load_verifier


def rule(code=0):
    return {"exit_code": {"graded": True, "line_count": 1,
                          "digest": "sha256:" + hashlib.sha256(str(code).encode()).hexdigest()}}


@pytest.mark.parametrize("args", [[], ["--help"], ["--version"], ["-v"],
                                 ["convert", "--help"], ["help", "convert"]])
def test_control_queries_and_bare_invocations_are_not_workloads(args):
    scenario = {"steps": [{"argv": ["{PROGRAM}", *args]}]}
    assert eligible_timing_steps(scenario, [rule()]) == []


def test_ordinary_inputs_verbose_flags_and_stdin_still_qualify():
    for step in ({"argv": ["{PROGRAM}", "convert", "input.json"]},
                 {"argv": ["{PROGRAM}", "-v", "input.json"]},
                 {"argv": ["{PROGRAM}"], "stdin": "{\"x\":1}"},
                 {"argv": ["{PROGRAM}", "--", "--help"]}):
        assert eligible_timing_steps({"steps": [step]}, [rule()]) == [0]
    assert timing_steps({"fixture": "inputs.tar", "steps": [{"argv": ["{PROGRAM}"]}]}) == [0]


def test_output_on_failed_path_and_ungraded_success_are_not_timing_evidence():
    scenario = {"steps": [{"argv": ["{PROGRAM}", "bad.json"]}]}
    assert eligible_timing_steps(scenario, [rule(2)]) == []
    ungraded = rule()
    ungraded["exit_code"]["graded"] = False
    assert eligible_timing_steps(scenario, [ungraded]) == []
    assert stages._pick_timed(["help", "error", "version"], worked=set()) == []


def test_real_process_freeze_holds_out_work_and_keeps_control_cases(tmp_path):
    program = tmp_path / "cli.py"
    program.write_text(
        "import sys\n"
        "arg=sys.argv[1]\n"
        "if arg in ('--help','--version'): print('control'); sys.exit(0)\n"
        "if arg=='bad': print('invalid document'); sys.exit(2)\n"
        "print(sum(range(int(arg))))\n")
    scenarios = [Scenario(str(i), [Step(["{PROGRAM}", str(i)])]) for i in range(4, 12)]
    scenarios += [Scenario(arg, [Step(["{PROGRAM}", arg])]) for arg in ('bad', '--help', '--version')]
    observer = SimpleNamespace(run=lambda spec, s: run_scenario(s, [sys.executable, str(program)]))
    source = SimpleNamespace(count=len(scenarios), draw=lambda n: scenarios[:n])
    corpus = stages.freeze(object(), observer, source, runs=2)
    assert corpus.usable
    assert corpus.timed and set(corpus.timed) <= {str(i) for i in range(4, 12)}
    assert {'bad', '--help', '--version'} <= set(corpus.expectations)
    assert len(set(corpus.expectations) & {str(i) for i in range(4, 12)}) >= 1
    source = SimpleNamespace(count=3, draw=lambda n: scenarios[-3:])
    invalid = stages.freeze(object(), observer, source, runs=2)
    assert not invalid.usable and not invalid.timed


def test_standalone_verifier_rejects_manually_inserted_help_timing(tmp_path, monkeypatch):
    verifier = load_verifier(tmp_path, monkeypatch, 'process')
    scenario = {'probe_id': 'help', 'steps': [{'argv': ['{PROGRAM}', '--help']}]}
    verifier.TIMED_EXPECTATIONS = {'help': [rule()]}
    speedup, note = verifier.measure_speed({'help': scenario}, ['help'], ['unused'], ['unused'], '', ())
    assert speedup == 0 and not verifier.TIMING_REPORT['usable']
    assert 'cannot be timed' in note


def test_input_ignored_by_help_only_program_cannot_become_timing(tmp_path, monkeypatch):
    from frf.observe.process.workload import control_output_signatures
    program = tmp_path / 'cli.py'
    program.write_text("print('Usage: cli convert <input>')\n")
    scenarios = [Scenario('help', [Step(['{PROGRAM}', '--help'])])]
    scenarios += [Scenario(str(i), [Step(['{PROGRAM}'], stdin=str(i))]) for i in range(5)]
    observer = SimpleNamespace(run=lambda spec, s: run_scenario(s, [sys.executable, str(program)]))
    source = SimpleNamespace(count=len(scenarios), draw=lambda n: scenarios[:n])
    corpus = stages.freeze(object(), observer, source, runs=2)
    assert not corpus.usable and not corpus.timed
    rules = {pid: [step.to_json() for step in values] for pid, values in corpus.expectations.items()}
    verifier = load_verifier(tmp_path, monkeypatch, 'process')
    verifier.CONTROL_OUTPUTS = control_output_signatures([s.to_json() for s in scenarios], rules)
    verifier.TIMED_EXPECTATIONS = {'0': rules['0']}
    speed, note = verifier.measure_speed({'0': scenarios[1].to_json()}, ['0'],
                                         ['unused'], ['unused'], '', ())
    assert speed == 0 and not verifier.TIMING_REPORT['usable']
    assert 'cannot be timed' in note


def test_timing_selection_spreads_families_and_input_content_and_preserves_grading():
    features = {
        'parse-large': {'family': 'parse', 'input_key': 'large', 'input_bytes': 100000},
        'parse-small': {'family': 'parse', 'input_key': 'small', 'input_bytes': 100},
        'encode-medium': {'family': 'encode', 'input_key': 'medium', 'input_bytes': 10000},
        'encode-small': {'family': 'encode', 'input_key': 'small', 'input_bytes': 100},
        'validate-other': {'family': 'validate', 'input_key': 'other', 'input_bytes': 2000},
        'validate-small': {'family': 'validate', 'input_key': 'small', 'input_bytes': 100},
        'single': {'family': 'single', 'input_key': 'huge', 'input_bytes': 1000000},
    }
    ids = list(features)
    chosen = stages._pick_timed(ids, worked=set(ids), features=features)
    assert chosen == ['parse-large', 'encode-medium', 'validate-other']
    assert stages._pick_timed(list(reversed(ids)), worked=set(ids), features=features) == chosen
    assert {features[pid]['family'] for pid in ids if pid not in chosen} == {'parse', 'encode', 'validate', 'single'}
