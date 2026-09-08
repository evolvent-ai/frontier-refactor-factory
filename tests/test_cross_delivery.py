import json

import pytest

from frf.core.scale import Spec, TaskForm
from frf.observe.call import package
from frf.observe.call.observation import Observation, Expectation
from frf.observe.call.stages import Corpus
from frf.observe.probes.schema import Param, Schema
from frf.scales.module import Material


@pytest.mark.parametrize('target', ['rust', 'go', 'cpp'])
@pytest.mark.parametrize('scale', ['module', 'kernel'])
def test_cross_delivery_keeps_source_but_only_supplies_target_launcher(tmp_path, target, scale):
    source = tmp_path / 'upstream.py'
    source.write_text('def entry(x):\n    return x + 5\n')
    material = Material('synthetic://cross', 'python', str(source), 'entry', 'integration fixture',
                        Schema([Param('int')]))
    spec = Spec('cross-task', scale, 'python', 'integration fixture', target_language=target,
                task_form=TaskForm.CROSS_LANGUAGE)
    baseline = Observation(True, 6)
    corpus = Corpus(expectations=[Expectation('one', baseline.digest(), 5)], inputs={'one': [1]})
    task = tmp_path / 'task'
    (task / 'environment').mkdir(parents=True)
    (task / 'environment/Dockerfile').write_text('FROM runtime\n')
    package.write_tests(str(task), corpus, spec=spec, material=material)
    assert (task / 'environment/original/upstream.py').read_bytes() == source.read_bytes()
    frozen = json.loads((task / 'tests/expectations.json').read_text())
    assert frozen['target_language'] == target
    assert 'build_target' in (task / 'tests/verify.py').read_text()
    assert 'python' not in (task / 'environment/run.sh').read_text()
    assert (task / 'environment/build.sh').is_file()
    context = json.loads((task / 'environment/task-interface.json').read_text())
    assert context['target_language'] == target
    assert context['build_commands'] == ['/app/build.sh']
    assert 'executes that product directly' in context['interface']
    assert 'target implementation is not provided' in ''.join(
        path.read_text() for path in (task / 'environment/implementation').rglob('*') if path.is_file())
    assert not (task / 'environment/subject.py').exists()
    assert (task / 'tests/reference/subject.py').exists()


def test_package_cross_delivery_retains_full_operations_and_external_package_source(tmp_path):
    from frf.scales.package import Material as PackageMaterial
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    (checkout / 'README.md').write_text('package documentation')
    distribution = tmp_path / 'distribution'
    distribution.mkdir()
    (distribution / '__init__.py').write_text('def first(value):\n    return value\n')
    names = ('first', 'second', 'third', 'fourth')
    material = PackageMaterial('synthetic://package', 'python', str(checkout), names, 'fixture',
        dispatch=tuple({'name': name, 'module': 'subject_package', 'symbol': name} for name in names),
        package_name='subject_package', package_root=str(distribution))
    spec = Spec('cross-package', 'package', 'python', 'fixture', target_language='rust',
                task_form=TaskForm.CROSS_LANGUAGE)
    corpus = Corpus(expectations=[Expectation('one', Observation(True, 1).digest(), 5)],
                    inputs={'one': ['first', 1]})
    task = tmp_path / 'task'
    (task / 'environment').mkdir(parents=True)
    package.write_tests(str(task), corpus, spec=spec, material=material)
    context = json.loads((task / 'environment/task-interface.json').read_text())
    assert context['operations'] == list(names)
    assert len(context['dispatch']) == 4
    assert (task / 'environment/original/README.md').read_text() == 'package documentation'
    assert (task / 'environment/original/subject_package/__init__.py').read_bytes() == (distribution / '__init__.py').read_bytes()
    assert not (task / 'environment/subject.py').exists()


def test_repo_cross_image_does_not_build_or_copy_source_executable(tmp_path):
    from frf.scales.repo import Repo, Material as RepoMaterial
    from frf.observe.process.stages import Corpus as ProcessCorpus
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'main.go').write_text('package main\nfunc main() {}\n')
    repo = Repo()
    repo._material = RepoMaterial('synthetic://repo', 'go', str(source))
    repo._spec = Spec('cross-repo', 'repo', 'go', 'fixture', target_language='rust',
                      build=[['go', 'build', '-o', '{ROOT}/program', '.']],
                      invoke=['{ROOT}/program'], task_form=TaskForm.CROSS_LANGUAGE)
    task = tmp_path / 'task'
    (task / 'environment').mkdir(parents=True)
    (task / 'environment/Dockerfile').write_text('FROM rust:1.90-bookworm\n')
    repo.write_tests(str(task), ProcessCorpus())
    text = (task / 'environment/Dockerfile').read_text()
    assert 'reference-build' not in text and 'RUN go build' not in text
    assert '/app/build.sh' in (task / 'environment/run.sh').read_text()
    context = json.loads((task / 'environment/task-interface.json').read_text())
    assert 'target executable receives' in context['interface']
    assert context['target_language'] == 'rust'
    assert (task / 'environment/main.go').exists()
    assert (task / 'tests/reference/main.go').exists()
    declaration = json.loads((task / 'tests/environment.json').read_text())
    assert declaration['reference_argv'] == ['/app/program']
