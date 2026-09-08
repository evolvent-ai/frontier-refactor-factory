"""All declared numeric array kinds are eligible; scalar/list schemas do not become kernels."""
import pytest

from frf.core.scale import Candidate, TaskForm
from frf.scales.kernel import Kernel, SHAPES
from frf.scales.module import Module


def candidate(kind):
    return Candidate('test://numeric/' + kind, 'kernel', 'python', 'test-index', {
        'source_path': '/unused/subject.py', 'symbol': 'reduce',
        'schema': {'params': [{'kind': kind, 'size': 'n'}]}})


@pytest.mark.parametrize('kind', ['int_array', 'float_array', 'complex_array'])
@pytest.mark.parametrize('form', [TaskForm.INPLACE, TaskForm.CROSS_LANGUAGE])
def test_spec_and_sampling_keep_each_numeric_kind(tmp_path, monkeypatch, kind, form):
    import frf.scales.module as module
    monkeypatch.setattr(module, 'generate_task_name', lambda *args, **kwargs: 'numeric-reduction')
    kernel = Kernel(workspace=str(tmp_path))
    if form == TaskForm.CROSS_LANGUAGE:
        kernel._target_language = 'rust'
    spec = kernel.specify(candidate(kind), task_form=form)
    source = kernel.probes(spec)
    assert spec.scale == 'kernel' and spec.task_form == form
    assert kernel._material.schema.params[0].kind == kind
    assert source.shapes == SHAPES
    assert spec.target_language == ('rust' if form == TaskForm.CROSS_LANGUAGE else '')
    probes = source.draw(9)
    assert [len(probe[0]) for probe in probes[6:9]] == [shape['n'] for shape in SHAPES]
    value = probes[8][0][0]
    assert (type(value) is int if kind == 'int_array' else
            type(value) is float if kind == 'float_array' else
            isinstance(value, list) and len(value) == 2 and all(type(v) is float for v in value))


def test_sourcing_does_not_filter_out_integer_kernels(monkeypatch):
    values = [candidate(kind) for kind in ('int', 'int_array', 'float_array', 'complex_array')]
    monkeypatch.setattr(Module, 'find', lambda self, budget: iter(values))
    assert [c.detail['schema']['params'][0]['kind'] for c in Kernel().find(3)] == [
        'int_array', 'float_array', 'complex_array']


@pytest.mark.parametrize('kind', ['int', 'float', 'string'])
def test_scalar_inputs_still_do_not_establish_a_kernel(kind):
    with pytest.raises(ValueError, match='not a numeric kernel'):
        Kernel()._locate(candidate(kind))
