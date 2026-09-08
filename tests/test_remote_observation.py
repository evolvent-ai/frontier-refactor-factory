"""Execute the serialized remote driver on owned fixtures, then verify the live path in E2B."""
import sys
from types import SimpleNamespace

import pytest

from frf.core.sandbox import LocalProcess, Result, SandboxError
from frf.observe.process.runner import Scenario, Step, run_remote_many, run_scenario


def test_remote_single_and_batch_preserve_cwd_stdin_environment_and_state(tmp_path):
    program = tmp_path / 'program.py'
    program.write_text('import os,sys,pathlib\n'
                       'p=pathlib.Path("result")\n'
                       'if sys.argv[1] == "write": p.write_text(sys.stdin.read())\n'
                       'print(os.getcwd())\nprint(os.environ["CASE"])\nprint(p.read_text())\n')
    invocation = [sys.executable, str(program)]
    scenarios = [Scenario(str(i), [Step(['{PROGRAM}', 'write'], cwd='nested', stdin='%s\\n 100%\n'),
                                  Step(['{PROGRAM}', 'read'], cwd='nested')],
                          environment={'CASE': str(i)}) for i in range(2)]
    backend = LocalProcess(str(tmp_path))
    backend.name = 'remote'
    expected = {s.probe_id: run_scenario(s, invocation) for s in scenarios}
    batch = run_remote_many(scenarios, backend=backend, remote_program=invocation, remote_fixtures=None)
    assert batch == expected
    single = run_scenario(scenarios[0], invocation, backend=backend, remote_program=invocation)
    assert single == expected['0']
    assert any('nested/result' in line for line in batch['0'][1].tree.lines)


def test_remote_timeout_is_per_step_and_missing_stdin_is_eof(tmp_path):
    program = tmp_path / 'program.py'
    program.write_text('import sys,time\n'
                       'if sys.argv[1] == "wait": time.sleep(10)\n'
                       'else: print(repr(sys.stdin.read()))\n')
    backend = LocalProcess(str(tmp_path))
    backend.name = 'remote'
    scenario = Scenario('bounded', [Step(['{PROGRAM}', 'wait']), Step(['{PROGRAM}', 'read'])])
    actual = run_scenario(scenario, [sys.executable, str(program)], backend=backend, timeout=0.2)
    assert actual[0].exit_code == -1
    assert actual[1].exit_code == 0
    assert actual[1].stdout.lines == ("''",)


def test_remote_driver_failure_is_not_a_subject_observation():
    backend = SimpleNamespace(push=lambda *a: None,
                              run=lambda *a, **kw: Result(1, '', 'Request timed out'))
    with pytest.raises(SandboxError, match='driver failed'):
        run_remote_many([Scenario('x', [Step(['{PROGRAM}'])])], backend=backend,
                        remote_program=['program'], remote_fixtures=None)
