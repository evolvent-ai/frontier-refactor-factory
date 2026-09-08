"""Input-dependence checks must distinguish actual processing from unchanged fixture bytes."""
import json
import sys
from types import SimpleNamespace

import pytest

from frf.core.scale import Spec
from frf.observe.process.runner import Scenario, Step, run_scenario
from frf.observe.process.sensitivity import interactive_failure, select_workloads
from frf.scales.repo import Material, Repo
from frf.source.repo_harvest import fixture_archive


@pytest.mark.parametrize('body,accepted', [
    ("print('fixed')", False),
    ("print(sys.argv[2])", False),
    ("pathlib.Path('output.txt').write_text('fixed')", False),
    ("print(pathlib.Path(sys.argv[2]).read_text().lower())", True),
    ("pathlib.Path('output.txt').write_text(pathlib.Path(sys.argv[2]).read_text().lower())", True),
    ("p=pathlib.Path(sys.argv[2]); p.write_text(p.read_text().lower())", True),
    ("import time; print(time.time_ns())", False),
])
def test_input_use_is_observed_not_inferred_from_success(tmp_path, body, accepted):
    program = tmp_path / 'program.py'
    program.write_text('import pathlib,sys\n' + body + '\n')
    for name in ('a.txt', 'b.txt'):
        (tmp_path / name).write_text('UPPER ' + name)
    fixtures = tmp_path / 'fixtures'
    archive = fixture_archive(str(tmp_path), ['a.txt', 'b.txt'], str(fixtures))
    scenarios = [Scenario(name, [Step(['{PROGRAM}', 'convert', name])], archive)
                 for name in ('a.txt', 'b.txt')]

    def run(scenario, timeout):
        return run_scenario(scenario, [sys.executable, str(program)],
                            fixtures_dir=str(fixtures), timeout=timeout)

    selected, report = select_workloads(scenarios, run, fixtures_dir=str(fixtures))
    assert bool(selected) is accepted, report
    assert not list(fixtures.glob('.frf-sensitivity-*'))
    if accepted:
        assert all(item['paired_repeats'] == 2 for item in report['tested'])


def test_repo_probes_uses_input_selection_in_normal_path(tmp_path):
    (tmp_path / 'go.mod').write_text('module example.invalid/tool\n')
    (tmp_path / 'README.md').write_text('```sh\ntool convert spec.txt output.txt\n```\n')
    data = tmp_path / 'testdata'
    data.mkdir()
    for index in range(12):
        (data / ('%02d.txt' % index)).write_text('CONTENT %d' % index)
    program = tmp_path / 'program.py'
    program.write_text("import sys,pathlib\n"
                       "if len(sys.argv)<3: print('Usage: tool convert input output'); sys.exit(0)\n"
                       "print(pathlib.Path(sys.argv[2]).read_text().lower())\n")
    repo = Repo()
    repo._material = Material(identity='test://tool', language='go', root=str(tmp_path),
                              invoke=['{ROOT}/program'])
    repo._spec = Spec('tool', 'repo', 'go', 'Convert documents')
    repo._observer = SimpleNamespace(run=lambda spec, scenario, timeout: run_scenario(
        scenario, [sys.executable, str(program)], fixtures_dir=repo._material.fixtures, timeout=timeout))
    source = repo.probes(repo._spec)
    review = json.loads((tmp_path / '.frf-workload-review.json').read_text())
    assert len(review['selected_workloads']) == 5
    assert source.count > len(review['selected_workloads'])
    assert all(item['paired_repeats'] == 2 for item in review['tested'] if item['result'] == 'sensitive')
    assert not list((tmp_path / '.frf-fixtures').glob('.frf-sensitivity-*'))


def test_failed_guesses_do_not_discard_later_working_inputs(tmp_path):
    program = tmp_path / 'program.py'
    program.write_text("import sys,pathlib\n"
                       "if sys.argv[1]=='bad': print('invalid'); sys.exit(2)\n"
                       "print(pathlib.Path(sys.argv[2]).read_text().lower())\n")
    (tmp_path / 'a.txt').write_text('CONTENT')
    (tmp_path / 'b.txt').write_text('OTHER CONTENT')
    fixtures = tmp_path / 'fixtures'
    archive = fixture_archive(str(tmp_path), ['a.txt', 'b.txt'], str(fixtures))
    scenarios = [Scenario(str(i), [Step(['{PROGRAM}', 'bad' if i < 8 else 'convert',
                                       'a.txt' if i % 2 == 0 else 'b.txt'])], archive)
                 for i in range(10)]
    selected, report = select_workloads(scenarios, lambda scenario, timeout: run_scenario(
        scenario, [sys.executable, str(program)], fixtures_dir=str(fixtures), timeout=timeout),
        fixtures_dir=str(fixtures))
    assert {scenario.probe_id for scenario in selected} == {'8', '9'}
    assert set(report['selected_workloads']) == {'8', '9'}


def test_documented_validator_rejects_wrong_input_type_for_permissive_transformer(tmp_path):
    program = tmp_path / 'program.py'
    program.write_text("import json,sys,pathlib\n"
                       "data=json.loads(pathlib.Path(sys.argv[3]).read_text())\n"
                       "if sys.argv[2]=='validate': sys.exit(0 if data.get('kind')=='document' else 2)\n"
                       "print(json.dumps(data,sort_keys=True))\n")
    for name, content in [('a.json', {'kind': 'document', 'v': 1}),
                          ('b.json', {'kind': 'document', 'v': 2}),
                          ('config.json', {'setting': 'not a document'})]:
        (tmp_path / name).write_text(json.dumps(content))
    fixtures = tmp_path / 'fixtures'
    archive = fixture_archive(str(tmp_path), ['a.json', 'b.json', 'config.json'], str(fixtures))
    scenarios = [Scenario(operation + name, [Step(['{PROGRAM}', 'doc', operation, name])], archive)
                 for operation in ('convert', 'validate') for name in ('a.json', 'b.json', 'config.json')]
    _, report = select_workloads(scenarios, lambda scenario, timeout: run_scenario(
        scenario, [sys.executable, str(program)], fixtures_dir=str(fixtures), timeout=timeout),
        fixtures_dir=str(fixtures))
    assert 'converta.json' in report['selected_workloads']
    assert 'convertb.json' in report['selected_workloads']
    assert 'convertconfig.json' not in report['selected_workloads']
    bad = next(item for item in report['tested'] if item['probe_id'] == 'convertconfig.json')
    assert bad['result'] == 'invalid-for-command-family'


def test_terminal_dependent_commands_are_not_benchmark_workloads():
    from frf.observe.process.observation import Observation, Stream
    assert interactive_failure([Observation(1, stderr=Stream.of('Error: could not open a new TTY'))])
    assert not interactive_failure([Observation(1, stderr=Stream.of('invalid document'))])


def test_interactive_failure_is_excluded_from_error_supplement():
    from frf.observe.process.observation import Observation, Stream
    scenarios = [Scenario(str(i), [Step(['{PROGRAM}', 'convert'], stdin=str(i + 1))])
                 for i in range(8)]
    scenarios.insert(0, Scenario('tty', [Step(['{PROGRAM}', 'explore'])]))
    def run(scenario, timeout):
        if scenario.probe_id == 'tty':
            return [Observation(1, stderr=Stream.of('could not open a new TTY'))]
        return [Observation(0, stdout=Stream.of(scenario.steps[0].stdin or 'empty'))]
    selected, report = select_workloads(scenarios, run, per_family=8)
    assert len(report['selected_workloads']) == 8
    assert 'tty' not in {scenario.probe_id for scenario in selected}
    assert next(item for item in report['tested'] if item['probe_id'] == 'tty')['result'] == 'interactive-required'


def test_timeouts_and_unstarted_programs_do_not_become_frozen_error_examples():
    from frf.observe.process.observation import Observation, Stream
    scenarios = [Scenario(str(i), [Step(['{PROGRAM}', 'convert'], stdin=str(i + 1))])
                 for i in range(8)]
    scenarios += [Scenario(str(code), [Step(['{PROGRAM}', 'slow'])]) for code in (-1, 127)]
    def run(scenario, timeout):
        if scenario.probe_id in ('-1', '127'):
            return [Observation(int(scenario.probe_id), stderr=Stream.of('did not finish'))]
        return [Observation(0, stdout=Stream.of(scenario.steps[0].stdin or 'empty'))]
    selected, report = select_workloads(scenarios, run, per_family=8)
    assert len(report['selected_workloads']) == 8
    assert not {'-1', '127'} & {scenario.probe_id for scenario in selected}
    assert all(item['result'] == 'execution-incomplete' for item in report['tested']
               if item['probe_id'] in ('-1', '127'))


@pytest.mark.parametrize('expire_after', [1, 2, 3])
def test_budget_exhaustion_preserves_an_honest_complete_review_record(monkeypatch, expire_after):
    from frf.observe.process import sensitivity
    from frf.observe.process.observation import Observation, Stream
    clock = [0]
    calls = []
    monkeypatch.setattr(sensitivity.time, 'monotonic', lambda: clock[0])
    def run(scenario, timeout):
        calls.append(scenario)
        if len(calls) == expire_after:
            clock[0] = 2
        return [Observation(0, stdout=Stream.of(scenario.steps[0].stdin or 'empty'))]
    scenario = Scenario('input', [Step(['{PROGRAM}', 'convert'], stdin='value')])
    selected, report = select_workloads([scenario], run, max_seconds=1)
    assert selected == []
    assert report['budget_exhausted']
    assert report['selected_workloads'] == []
    assert report['tested'][0]['result'] in ('verification-incomplete', 'input-sensitive-once')
    assert 'paired_repeats' not in report['tested'][0]
