"""Select successful workloads whose observations actually depend on their supplied input."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from collections import Counter
from dataclasses import replace

from .workload import command_family, control_step, timing_steps


def interactive_failure(observations):
    """Non-interactive benchmark runners must reject commands that require a terminal."""
    markers = ('/dev/tty', 'could not open a new tty', 'no such device or address',
               'stdin is not a terminal', 'not a tty')
    return any(any(marker in line.lower() for marker in markers)
               for observed in observations for line in observed.stderr.lines)


def _file_value(data, mode):
    return 'file %s %d %s' % ('x' if mode & 0o111 else '-', len(data), hashlib.sha256(data).hexdigest())


def _signature(observations, input_path=None, input_value=None):
    result = []
    for observed in observations:
        tree = []
        for line in observed.tree.lines:
            path, value = json.loads(line)
            # Changing an untouched fixture is not evidence that the program consumed it.
            if path == input_path and value == input_value:
                value = 'unchanged-input'
            tree.append((path, value))
        result.append((observed.exit_code, observed.stdout.lines, observed.stderr.lines, tuple(tree)))
    return result


def _perturb(scenario, fixtures_dir, created):
    for index, step in enumerate(scenario.steps):
        if step.stdin:
            steps = list(scenario.steps)
            steps[index] = replace(step, stdin='')
            return replace(scenario, steps=steps), None, None, None, 'stdin'
    if not scenario.fixture or not fixtures_dir:
        return None
    original = os.path.join(fixtures_dir, scenario.fixture)
    with tarfile.open(original) as archive:
        members = {m.name: m for m in archive.getmembers() if m.isfile()}
        input_path = next((os.path.normpath(str(arg).split('=', 1)[-1])
                           for step in scenario.steps for arg in step.argv[1:]
                           if os.path.normpath(str(arg).split('=', 1)[-1]) in members), None)
        if input_path is None:
            return None
        member = members[input_path]
        content = archive.extractfile(member).read()
        altered = b'' if content else b'FRF input sensitivity probe\n'
        key = hashlib.sha256((scenario.fixture + '\0' + input_path).encode()).hexdigest()[:20]
        name = '.frf-sensitivity-' + key + '.tar.gz'
        destination = os.path.join(fixtures_dir, name)
        if name not in created:
            with tarfile.open(destination, 'w:gz') as changed:
                for item in archive.getmembers():
                    data = archive.extractfile(item).read() if item.isfile() else None
                    if item.name == input_path:
                        data = altered
                        item.size = len(data)
                    changed.addfile(item, io.BytesIO(data) if data is not None else None)
            created.add(name)
        return (replace(scenario, fixture=name), input_path,
                _file_value(content, member.mode), _file_value(altered, member.mode), input_path)


def select_workloads(scenarios, run, *, fixtures_dir=None, sync_fixtures=None,
                     cleanup_fixtures=None, max_seconds=360, max_workloads=48, per_family=5):
    """Keep input-sensitive successful execution plus bounded real error/control cases.

    `run` receives an explicit per-command timeout. This is a supply/quality check; it does not
    establish repository coverage, timing significance, or adversarial evaluator isolation.
    """
    started = time.monotonic()
    unique = {}
    for scenario in scenarios:
        data = scenario.to_json()
        data.pop('probe_id')
        unique.setdefault(json.dumps(data, sort_keys=True), scenario)
    scenarios = list(unique.values())
    created, prepared, features = set(), {}, {}
    validators, validation = {}, {}
    report = {'tested': [], 'selected_workloads': [], 'families': {}, 'budget_exhausted': False}
    accepted, errors, controls = [], [], []
    families = Counter()
    try:
        for scenario in scenarios:
            if timing_steps(scenario.to_json()):
                prepared[scenario.probe_id] = _perturb(scenario, fixtures_dir, created)
                args = scenario.steps[0].argv
                if (len(scenario.steps) == 1 and len(args) >= 3 and args[-2] == 'validate'
                        and prepared[scenario.probe_id] is not None
                        and prepared[scenario.probe_id][1] == os.path.normpath(args[-1])):
                    validators.setdefault(tuple(args[:-2]), scenario)
            perturbation = prepared.get(scenario.probe_id)
            path = perturbation[1] if perturbation else None
            data = scenario.to_json()
            if path is not None:
                _, _, length, digest = perturbation[2].split()
                length = int(length)
            else:
                payload = ''.join(step.stdin or '' for step in scenario.steps).encode()
                length, digest = len(payload), hashlib.sha256(payload).hexdigest()
            features[scenario.probe_id] = {'family': command_family(data, path),
                                           'input_key': digest, 'input_bytes': length}
        if created and sync_fixtures is not None:
            sync_fixtures()
        pending, attempts, used_inputs = list(scenarios), Counter(), set()
        while pending:
            def priority(scenario):
                feature = features[scenario.probe_id]
                return (any(control_step(step.to_json()) for step in scenario.steps),
                        -attempts[feature['family']], feature['input_key'] not in used_inputs,
                        feature['input_bytes'], scenario.probe_id)
            scenario = max(pending, key=priority)
            pending.remove(scenario)
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                report['budget_exhausted'] = True
                break
            feature = features[scenario.probe_id]
            family = feature['family']
            is_control = any(control_step(step.to_json()) for step in scenario.steps)
            if not is_control and (families[family] >= per_family or len(accepted) >= max_workloads):
                continue
            attempts[family] += 1
            actual = run(scenario, min(10.0, remaining))
            record = {'probe_id': scenario.probe_id, 'argv': [step.argv for step in scenario.steps],
                      'result': 'verification-incomplete', **feature}
            report['tested'].append(record)
            if not actual or len(actual) != len(scenario.steps):
                record['result'] = 'missing-observation'
                continue
            if any(one.exit_code in (-1, 127) for one in actual):
                record['result'] = 'execution-incomplete'
                continue
            if is_control:
                record['result'] = 'control'
                if len(controls) < 4:
                    controls.append(scenario)
                continue
            if any(one.exit_code != 0 for one in actual):
                record['result'] = 'error'
                interactive = interactive_failure(actual)
                if interactive:
                    record['result'] = 'interactive-required'
                if not interactive and len(errors) < 8:
                    errors.append(scenario)
                continue
            perturbation = prepared.get(scenario.probe_id)
            if perturbation is None:
                record['result'] = 'input-dependence-unproven'
                continue
            modified, path, before_value, modified_value, channel = perturbation
            # Reuse a repository-documented validator for the same command family and fixture.
            # This catches permissive transformers accepting unrelated document/config formats.
            args = scenario.steps[0].argv
            validator = next((value for prefix, value in validators.items()
                              if tuple(args[:len(prefix)]) == prefix
                              and value.fixture == scenario.fixture
                              and value.environment == scenario.environment), None)
            if validator is not None and path is not None:
                key = (tuple(validator.steps[0].argv[:-1]), scenario.fixture, path)
                if key not in validation:
                    remaining = max_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        report['budget_exhausted'] = True
                        break
                    probe = replace(validator, steps=[replace(validator.steps[0],
                                    argv=validator.steps[0].argv[:-1] + [path])])
                    validation[key] = run(probe, min(10.0, remaining))
                checked = validation[key]
                record['validator_argv'] = list(key[0]) + [path]
                record['input_validated'] = bool(checked) and all(one.exit_code == 0 for one in checked)
                if not record['input_validated']:
                    record['result'] = 'invalid-for-command-family'
                    continue
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                report['budget_exhausted'] = True
                break
            changed = run(modified, min(10.0, remaining))
            if len(changed) != len(actual) or any(one.exit_code in (-1, 127) for one in changed):
                record['result'] = 'perturbation-inconclusive'
                continue
            sensitive = _signature(actual, path, before_value) != _signature(changed, path, modified_value)
            record.update(result='input-sensitive-once' if sensitive else 'input-ignored', input=channel)
            if sensitive:
                # Repeat both sides so a timestamp or random message cannot masquerade as input use.
                remaining = max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    report['budget_exhausted'] = True
                    break
                repeated = run(scenario, min(10.0, remaining))
                remaining = max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    report['budget_exhausted'] = True
                    break
                repeated_change = run(modified, min(10.0, remaining))
                if (_signature(repeated, path, before_value) != _signature(actual, path, before_value)
                        or _signature(repeated_change, path, modified_value) !=
                        _signature(changed, path, modified_value)):
                    record['result'] = 'unstable-input-evidence'
                    continue
                record.update(result='sensitive', paired_repeats=2)
                accepted.append(scenario)
                families[family] += 1
                used_inputs.add(feature['input_key'])
        report.update(selected_workloads=[s.probe_id for s in accepted], families=dict(families),
                      seconds=round(time.monotonic() - started, 3))
        # Error paths supplement a corpus that works; they must not dominate its grading.
        selected = accepted + errors[:min(8, len(accepted) // 4)] + controls
        return selected if len(accepted) >= 2 else [], report
    finally:
        for name in created:
            os.unlink(os.path.join(fixtures_dir, name))
        if created and cleanup_fixtures is not None:
            cleanup_fixtures(sorted(created))
