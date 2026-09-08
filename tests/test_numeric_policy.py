import json
import math
import random
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from frf.observe.call import observation as obs
from frf.observe.call.stages import Corpus, _score_trivial
from frf.observe.compare.numeric import compare_numeric, validate_numeric_policy
from frf.observe.compare.numeric_policy import select_numeric_policy
from frf.observe.probes.schema import Param, Schema
from tests.test_timing_protocol import load_verifier

POLICY = {'kind': 'float-tolerance', 'rtol': 1e-9, 'atol': 1e-12, 'equal_nan': True}


@pytest.mark.parametrize('reference,actual,same', [
    (1.0, 1.0 + 5e-10, True), (1.0, 1.0 + 5e-8, False),
    (0.0, 5e-13, True), (0.0, 5e-11, False),
    (10**18, 10**18 + 1, False), (1, 1.0, False), (1.0, 1, True),
    (True, 1, False), (False, 0.0, False), (float('nan'), float('nan'), True),
    (float('nan'), 0.0, False), (float('inf'), float('inf'), True),
    (float('inf'), -float('inf'), False), (1.0, float('inf'), False),
    ([1.0, [2.0]], [1.0, 2.0], False),
    ({'dtype': 'float64', 'value': 1.0}, {'dtype': 'float32', 'value': 1.0}, False),
    ({'value': [1.0]}, {'value': [1.0 + 5e-10]}, True),
    ({'value': 1.0}, {'other': 1.0}, False), (1.0, 10**1000, False),
])
def test_numeric_contract_preserves_structure_types_and_special_values(reference, actual, same):
    assert compare_numeric(reference, actual, POLICY)[0] is same


@pytest.mark.parametrize('field,value', [('rtol', -1), ('atol', float('nan')),
                                        ('rtol', float('inf')), ('rtol', 10**1000),
                                        ('atol', True), ('equal_nan', 1)])
def test_invalid_policy_is_not_silently_accepted(field, value):
    with pytest.raises(ValueError):
        validate_numeric_policy(dict(POLICY, **{field: value}))


def test_reordered_floating_reduction_passes_but_real_error_does_not():
    rng = random.Random(42)
    values = [rng.uniform(-1, 1) for _ in range(10000)]
    def accumulate(items):
        value = 0.0
        for item in items:
            value += item
        return value
    original, reordered = accumulate(values), accumulate(reversed(values))
    assert original != reordered
    assert compare_numeric(original, reordered, POLICY)[0]
    assert not compare_numeric(original, reordered + 1e-3, POLICY)[0]


def material(tmp_path, dtype='float64'):
    source = tmp_path / 'subject.py'
    source.write_text('def reduce(values):\n    return sum(values)\n')
    return SimpleNamespace(source_path=str(source), symbol='reduce',
                           schema=Schema([Param('float_array', dtype=dtype)]))


def test_upstream_assertion_overrides_precision_default(tmp_path):
    subject = material(tmp_path)
    test = tmp_path / 'test_subject.py'
    test.write_text('import numpy as np\nfrom subject import reduce as selected\n'
                    'def test_reduce():\n'
                    '    np.testing.assert_allclose(selected([1.0]), 1.0, rtol=1e-6, atol=2e-8, equal_nan=False)\n')
    policy = select_numeric_policy(subject)
    assert (policy['rtol'], policy['atol'], policy['equal_nan']) == (1e-6, 2e-8, False)
    assert policy['basis'] == 'upstream assertion'
    assert policy['evidence'][0]['file'] == 'test_subject.py'
    assert policy['evidence'][0]['line'] == 4
    assert not compare_numeric(float('nan'), float('nan'), policy)[0]


def test_unrelated_or_custom_assertion_cannot_supply_tolerance(tmp_path):
    subject = material(tmp_path, 'float32')
    (tmp_path / 'test_subject.py').write_text(
        'import numpy as np\nfrom unrelated import reduce\n'
        'np.testing.assert_allclose(reduce([1.0]), 1.0, rtol=0.5, atol=2.0)\n')
    policy = select_numeric_policy(subject)
    assert policy['basis'] == 'declared-precision default'
    assert policy['rtol'] == 1e-5


def test_upstream_result_variable_and_constant_tolerance_are_resolved(tmp_path):
    subject = material(tmp_path)
    (tmp_path / 'test_subject.py').write_text(
        'import numpy as np\nfrom subject import reduce\nTOL = 3e-8\n'
        'def test_reduce():\n    result = reduce([1.0])\n'
        '    np.testing.assert_allclose(result, 1.0, atol=TOL)\n')
    policy = select_numeric_policy(subject)
    assert policy['basis'] == 'upstream assertion'
    assert policy['rtol'] == 1e-7 and policy['atol'] == 3e-8


def test_overwritten_result_or_other_test_scope_does_not_bind_an_assertion(tmp_path):
    subject = material(tmp_path)
    (tmp_path / 'test_subject.py').write_text(
        'import numpy as np\nfrom subject import reduce\n'
        'def test_first():\n    result = reduce([1.0])\n    result = 99.0\n'
        '    np.testing.assert_allclose(result, 99.0, rtol=0.5, atol=1.0)\n'
        'def test_second():\n    np.testing.assert_allclose(result, 1.0, rtol=0.5, atol=1.0)\n')
    assert select_numeric_policy(subject)['basis'] == 'declared-precision default'


def test_conflicting_upstream_contract_requires_explicit_resolution(tmp_path):
    subject = material(tmp_path)
    (tmp_path / 'test_subject.py').write_text(
        'from numpy.testing import assert_allclose\nfrom subject import reduce\n'
        'assert_allclose(reduce([1.0]), 1.0, rtol=1e-6, atol=0.0)\n'
        'assert_allclose(reduce([2.0]), 2.0, rtol=1e-8, atol=0.0)\n')
    with pytest.raises(ValueError, match='conflicting'):
        select_numeric_policy(subject)


def test_numerical_floor_uses_the_published_tolerance():
    baseline = obs.Observation(True, 1e-14)
    expectation = obs.Expectation('one', baseline.digest(), 5)
    corpus = Corpus(expectations=[expectation], inputs={'one': [1]}, numeric_policy=POLICY,
                    reference_values={'one': baseline})
    assert _score_trivial(None, None, corpus, 'returns-zero') == (1, 1)
    changed = obs.Observation(True, 2.0)
    assert obs.grade(expectation, baseline, reference=changed, policy=POLICY)[0] == 0


def test_mutation_check_skips_allowed_roundoff_and_checks_a_real_difference():
    from frf.observe.call.stages import _perturb
    from contextlib import contextmanager
    baseline = obs.Observation(True, 1.0)
    expectation = obs.Expectation('one', baseline.digest(), 5)
    corpus = Corpus(expectations=[expectation], inputs={'one': [1]}, numeric_policy=POLICY,
                    reference_values={'one': baseline})
    attempts = []
    @contextmanager
    def subject(_spec, *, mutated, attempt):
        attempts.append(attempt)
        value = 1.0 + (5e-10 if attempt == 0 else 1e-3)
        yield SimpleNamespace(call=lambda *_args: obs.Observation(True, value))
    assert _perturb(SimpleNamespace(subject=subject), None, corpus) == (True, True)
    assert attempts == [0, 1]


def make_subject(root, expression):
    root.mkdir(parents=True, exist_ok=True)
    script = root / 'serve.py'
    script.write_text('import json,sys\nfor line in sys.stdin:\n'
                      '    request=json.loads(line)\n'
                      '    x=float(request["args"][0])\n'
                      '    print(json.dumps({"id":request["id"],"ok":True,"value":' + expression + '}),flush=True)\n')
    launcher = root / 'run.sh'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-u', str(script)]) + '\n')
    launcher.chmod(0o755)


@pytest.mark.parametrize('candidate,reference,passed', [('x+5e-10', 'x', 2), ('x+1e-3', 'x', 0),
                                                       ('x+5e-10', 'x+5e-10', 0)])
def test_generated_verifier_compares_numerically_without_refreezing(tmp_path, monkeypatch,
                                                                  candidate, reference, passed):
    verifier = load_verifier(tmp_path, monkeypatch, 'call')
    workspace = tmp_path / 'candidate'
    make_subject(workspace, candidate)
    make_subject(tmp_path / 'reference', reference)
    frozen = {'numeric_policy': POLICY, 'comparison': 'envelope', 'timed': [],
              'probes': {'a': [1.0], 'b': [2.0]},
              'graded': [obs.Expectation(name, obs.Observation(True, value).digest(), 5).to_json()
                         for name, value in [('a', 1.0), ('b', 2.0)]]}
    expectations = tmp_path / 'expectations.json'
    expectations.write_text(json.dumps(frozen))
    before = expectations.read_bytes()
    reward = tmp_path / 'reward.json'
    import os
    done = subprocess.run([sys.executable, verifier.__file__, '--task-root', str(tmp_path),
                           '--workspace', str(workspace)], capture_output=True, text=True, timeout=20,
                          env=dict(os.environ, REWARD_PATH=str(reward)))
    assert done.returncode in (0, 1), done.stderr
    result = json.loads(reward.read_text())
    assert result['correctness_passed'] == passed
    assert result['correctness_total'] == 2
    assert expectations.read_bytes() == before
    if reference != 'x':
        assert 'reference no longer matches' in result['note']


def test_invalid_numeric_policy_cannot_leave_a_stale_reward(tmp_path, monkeypatch):
    import os
    verifier = load_verifier(tmp_path, monkeypatch, 'call')
    (tmp_path / 'expectations.json').write_text(json.dumps({'numeric_policy': dict(POLICY, rtol=-1)}))
    reward = tmp_path / 'reward.json'
    reward.write_text('{"reward": 99, "correct": true}')
    done = subprocess.run([sys.executable, verifier.__file__, '--task-root', str(tmp_path)],
                          capture_output=True, text=True, timeout=20,
                          env=dict(os.environ, REWARD_PATH=str(reward)))
    assert done.returncode != 0
    assert not reward.exists()


@pytest.mark.parametrize('difference,valid', [(5e-10, True), (1e-3, False)])
def test_timed_responses_use_the_same_numeric_policy(tmp_path, monkeypatch, difference, valid):
    verifier = load_verifier(tmp_path, monkeypatch, 'call')
    (tmp_path / 'reference').mkdir()
    (tmp_path / 'reference/run.sh').touch()

    class Subject:
        def __init__(self, argv, cwd, role='candidate'):
            self.role = role
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def activate(self):
            pass
        def call(self, _probe):
            return {'ok': True, 'value': 1.0 + (difference if self.role == 'candidate' else 0)}

    measured = []
    def measure(reference, candidate, _draw, shapes):
        measured.append(shapes)
        reference('one')
        candidate('one')
        return SimpleNamespace(speedup=1.0, note='', to_json=lambda: {'usable': True})
    monkeypatch.setattr(verifier, 'Subject', Subject)
    monkeypatch.setattr(verifier, 'measure', measure)
    monkeypatch.setattr(verifier, 'TIMED_REPEATS', 1)
    frozen = {'numeric_policy': POLICY, 'timed': ['one'], 'probes': {'one': [1.0]},
              'timed_expectations': {'one': obs.Observation(True, 1.0).digest()}}
    verifier.measure_speed(frozen, ['candidate'], [str(tmp_path / 'reference/run.sh')],
                           SimpleNamespace(workspace=str(tmp_path)), str(tmp_path))
    assert verifier.TIMING_REPORT['usable'] is valid
    assert bool(measured) is valid
