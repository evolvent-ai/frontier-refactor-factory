"""Evidence must exercise the supplied implementation and the channel it claims to test."""
import sys
from types import SimpleNamespace

from frf.core import adequacy
from frf.core.scale import Spec
from frf.observe.process import observation as obs, stages
from frf.observe.process.runner import Scenario, Step
from frf.scales.repo import Material, Observer


def test_real_process_alternatives_are_executed_and_temporary_trees_cleaned(tmp_path, monkeypatch):
    program = tmp_path / 'program.py'
    program.write_text("print('reference')\n")
    observer = Observer(Material('test://source', 'python', str(tmp_path)))
    observer._program = [sys.executable, str(program)]
    scenarios = [Scenario('work', [Step(['{PROGRAM}', 'input'])])]
    created = []
    from frf.core import scratch
    original = scratch.mkdtemp
    def track(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(path)
        return path
    monkeypatch.setattr(scratch, 'mkdtemp', track)
    reference = observer.run_all(None, scenarios)['work'][0]
    empty = observer.run_all(None, scenarios, submission='#!/bin/sh\nexit 0\n')['work'][0]
    wrong = observer.run_all(None, scenarios, mutated='stdout')['work'][0]
    assert reference.stdout.lines == ('reference',)
    assert empty.stdout.lines == ()
    assert wrong.stdout.lines == ('frf-mutant', 'reference')
    assert wrong.exit_code == reference.exit_code == 0
    from pathlib import Path
    assert all(not Path(path).exists() for path in created)
    assert program.read_text() == "print('reference')\n"


def test_different_channel_failure_cannot_vouch_for_a_blind_target_channel(monkeypatch):
    reference = obs.Observation(0, stdout=obs.Stream.of('reference'), stderr=obs.Stream.of(''))
    changed = obs.Observation(2, stdout=obs.Stream.of('changed'), stderr=obs.Stream.of(''))
    expectation = obs.freeze(0, [reference, reference])
    corpus = stages.Corpus(scenarios=[Scenario('x', [Step(['{PROGRAM}', 'input'])])],
                           expectations={'x': [expectation]})
    observer = SimpleNamespace(run_all=lambda *args, **kwargs: {'x': [changed]})
    actual_grade = obs.grade
    def blind_stdout(expected, actual):
        passed, total, reasons = actual_grade(expected, actual)
        if expected.stdout.graded:
            passed += 1
        return passed, total, reasons
    monkeypatch.setattr(obs, 'grade', blind_stdout)
    assert stages._perturb(observer, None, corpus, 'stdout') == (True, False)


def test_repair_refreezes_all_runs_and_masks_unstable_channels(monkeypatch):
    calls = []
    corpus = stages.Corpus(runs=5)
    observer = SimpleNamespace(run=lambda spec, scenario: (
        calls.append(scenario) or [obs.Observation(0, stdout=obs.Stream.of(str(len(calls))))]))
    def exercise(spec, observer, corpus, **kwargs):
        kwargs['refreeze'](corpus, 'repaired', {'cmd': ['{PROGRAM}', 'input']})
        return corpus
    monkeypatch.setattr(adequacy, 'repair', exercise)
    stages.audit(Spec('x', 'repo', 'python', ''), observer, corpus)
    assert len(calls) == 5
    assert not corpus.expectations['repaired'][0].stdout.graded


def test_shell_printing_the_program_token_is_not_a_subject_call():
    observed = obs.Observation(0, stdout=obs.Stream.of('a program path'))
    corpus = stages.Corpus(scenarios=[Scenario('x', [Step(['echo', '{PROGRAM}'])])],
                           expectations={'x': [obs.freeze(0, [observed, observed])]})
    assert stages._steps_touching_subject(corpus) == (0, 1)


def test_final_allowed_repair_is_assessed_before_refusal(monkeypatch):
    corpus = SimpleNamespace(probes=1, scenarios=[], usable=True)
    measurements = []
    def measure(*args):
        measurements.append(corpus.probes)
        return adequacy.Reach(reached=1 if corpus.probes == 1 else 5, total=10,
                              dark=('uncovered.py',), backend='test')
    observer = SimpleNamespace(coverage=lambda: SimpleNamespace(measure=measure))
    monkeypatch.setattr(adequacy, '_filter_contractual_dark', lambda *args: ['uncovered.py'])
    monkeypatch.setattr(adequacy, '_propose_probes_for_dark', lambda *args: [{}])
    def refreeze(corpus, probe_id, data):
        corpus.probes += 1
    result = adequacy.repair(None, observer, corpus, lambda name: (0, 1),
                             max_iterations=1, refreeze=refreeze)
    assert measurements == [1, 2]
    assert result.usable and result.adequacy['ok']
