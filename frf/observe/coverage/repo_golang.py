"""Go coverage of the real repository executable, driven by process scenarios."""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import replace

from ...core.sandbox import Result
from ..process.runner import run_remote_many, run_scenario
from . import spans


def instrument_command(command, binary, module):
    args, index = [], 2
    while index < len(command):
        value = command[index]
        if value in ('-o', '-coverpkg', '-covermode'):
            index += 2
            continue
        if value == '-cover' or value.startswith(('-o=', '-coverpkg=', '-covermode=')):
            index += 1
            continue
        args.append(value)
        index += 1
    return [command[0], 'build', '-cover', '-covermode=atomic', '-coverpkg=' + module + '/...',
            '-o', binary] + args


def parse_profile(body, module):
    per_file = {}
    for line in body.splitlines():
        match = re.fullmatch(r'(.+):(\d+)\.\d+,(\d+)\.\d+\s+\d+\s+(\d+)', line)
        if not match:
            continue
        path, first, last, hits = match.groups()
        if not path.startswith(module + '/'):
            continue
        relative = path[len(module) + 1:]
        executed, executable = per_file.setdefault(relative, (set(), set()))
        lines = set(range(int(first), int(last) + 1))
        executable.update(lines)
        if int(hits):
            executed.update(lines)
    return per_file


class RepoGoCoverage:
    name = 'go-repo-cover'

    def __init__(self, observer):
        self.observer = observer
        self.last_report = {}

    def _run(self, args, root, timeout=600):
        backend = self.observer._backend
        if backend is not None:
            return backend.run(args, workdir=root, timeout=timeout)
        done = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=timeout)
        return Result(done.returncode, done.stdout, done.stderr)

    def measure(self, spec, probes):
        observer = self.observer
        root = observer._remote_root or observer.material.root
        room = '/tmp/frf-repo-cover-' + uuid.uuid4().hex[:12]
        self.last_report = {'status': 'unmeasured', 'scope':
                            'repository-owned Go packages linked into the declared executable'}
        try:
            command = next((list(cmd) for cmd in observer.material.build
                            if len(cmd) >= 2 and os.path.basename(cmd[0]) == 'go' and cmd[1] == 'build'), None)
            if command is None:
                self.last_report['reason'] = 'no direct Go build command to instrument'
                return spans.unmeasured(self.name)
            module_result = self._run([command[0], 'mod', 'edit', '-json'], root, 30)
            if not module_result.ok:
                raise RuntimeError('cannot inspect the repository module: ' + module_result.tail(400))
            module = json.loads(module_result.stdout)['Module']['Path']
            counter = room + '/counters'
            self._run(['mkdir', '-p', counter], root, 30)
            self._run(['chmod', '-R', 'a+rwx', room], root, 30)
            command = [part.replace('{ROOT}', root) for part in command]
            binary = room + '/program'
            built = self._run(instrument_command(command, binary, module), root)
            if not built.ok:
                raise RuntimeError('coverage build failed: ' + built.tail(500))
            scenarios = [replace(probe, environment=dict(probe.environment, GOCOVERDIR=counter))
                         for probe in probes]
            program = observer._restricted([binary])
            if observer._remote_root:
                results = run_remote_many(scenarios, backend=observer._backend, remote_program=program,
                                          remote_fixtures=observer._remote_fixtures or None,
                                          exclude=observer.material.exclude)
            else:
                results = {s.probe_id: run_scenario(s, program, fixtures_dir=observer.material.fixtures,
                                                    exclude=observer.material.exclude, timeout=30)
                           for s in scenarios}
            if any(not results.get(s.probe_id) or any(o.exit_code in (-1, 127) for o in results[s.probe_id])
                   for s in scenarios):
                raise RuntimeError('instrumented program did not complete every scenario')
            profile = room + '/coverage.txt'
            converted = self._run([command[0], 'tool', 'covdata', 'textfmt', '-i=' + counter, '-o=' + profile], root, 60)
            if not converted.ok:
                raise RuntimeError('coverage counters could not be read: ' + converted.tail(400))
            captured = self._run(['cat', profile], root, 30)
            if not captured.ok:
                raise RuntimeError('coverage profile is missing')
            files = parse_profile(captured.stdout, module)
            reach = spans.assemble(self.name, files)
            self.last_report.update(status='measured' if reach.measured else 'unmeasured',
                                    module=module, reach=reach.to_json(), profile=captured.stdout,
                                    scenario_exit_codes={pid: [o.exit_code for o in observations]
                                                         for pid, observations in results.items()},
                                    files={name: {'reached': len(hit), 'total': len(total)}
                                           for name, (hit, total) in files.items()})
            return reach
        except Exception as error:
            self.last_report['reason'] = str(error)[:700]
            return spans.unmeasured(self.name)
        finally:
            try:
                cleaned = self._run(['rm', '-rf', room], root, 30)
                self.last_report['cleanup_ok'] = cleaned.ok
            except Exception as error:
                self.last_report['cleanup_ok'] = False
                self.last_report['cleanup_error'] = type(error).__name__
