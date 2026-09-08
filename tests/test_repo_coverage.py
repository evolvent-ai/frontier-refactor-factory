"""Repository coverage uses the native build and process corpus, never a call shim."""
import json
from types import SimpleNamespace

import pytest

from frf.core.sandbox import Result
from frf.observe.coverage import repo_golang
from frf.observe.process.observation import Observation
from frf.observe.process.runner import Scenario, Step


def test_profile_keeps_relative_paths_and_excludes_dependencies():
    profile = ('mode: atomic\nexample/tool/a/main.go:3.1,5.2 2 1\n'
               'example/tool/a/main.go:5.1,8.2 3 0\n'
               'example/tool/b/main.go:3.1,4.2 1 1\n'
               'example/external/main.go:1.1,999.2 9 1\n')
    files = repo_golang.parse_profile(profile, 'example/tool')
    assert set(files) == {'a/main.go', 'b/main.go'}
    assert files['a/main.go'] == ({3, 4, 5}, {3, 4, 5, 6, 7, 8})


@pytest.mark.parametrize('build_ok', [True, False])
def test_remote_coverage_preserves_reference_and_records_real_or_missing_measurement(monkeypatch, build_ok):
    calls = []
    original_build = ['go', 'build', '-tags', 'fast', '-o', './program', './cmd/tool']
    def run(args, **kwargs):
        calls.append(args)
        if args[0:3] == ['go', 'mod', 'edit']:
            return Result(0, json.dumps({'Module': {'Path': 'example/tool'}}), '')
        if args[0:2] == ['go', 'build'] and not build_ok:
            return Result(1, '', 'compile failed')
        if args[0] == 'cat':
            return Result(0, 'mode: atomic\nexample/tool/core.go:1.1,5.2 3 2\n', '')
        return Result(0, '', '')
    backend = SimpleNamespace(name='remote', run=run)
    observer = SimpleNamespace(_backend=backend, _remote_root='/remote/source',
                               _remote_fixtures='/remote/fixtures', _program=['/remote/source/program'],
                               material=SimpleNamespace(root='/local/source', build=[original_build], exclude=()),
                               _restricted=lambda args: args)
    scenario = Scenario('work', [Step(['{PROGRAM}', 'convert', 'input.json'])])
    def remote_many(scenarios, **kwargs):
        assert kwargs['backend'] is backend
        assert kwargs['remote_program'][0].startswith('/tmp/frf-repo-cover-')
        assert scenarios[0].environment['GOCOVERDIR'].endswith('/counters')
        return {'work': [Observation(0)]}
    monkeypatch.setattr(repo_golang, 'run_remote_many', remote_many)
    monkeypatch.setattr(repo_golang.subprocess, 'run', lambda *a, **kw: pytest.fail('host execution'))
    meter = repo_golang.RepoGoCoverage(observer)
    reach = meter.measure(None, [scenario])
    assert reach.measured is build_ok
    if build_ok:
        assert reach.reached == 5 and reach.total == 5
        assert meter.last_report['files']['core.go']['reached'] == 5
    else:
        assert 'compile failed' in meter.last_report['reason']
    build = next(args for args in calls if args[:2] == ['go', 'build'])
    assert build[-3:] == ['-tags', 'fast', './cmd/tool']
    assert build[build.index('-o') + 1] != '/remote/source/program'
    assert original_build == ['go', 'build', '-tags', 'fast', '-o', './program', './cmd/tool']
    assert observer._program == ['/remote/source/program'] and scenario.environment == {}
    assert calls[-1][0:2] == ['rm', '-rf']
