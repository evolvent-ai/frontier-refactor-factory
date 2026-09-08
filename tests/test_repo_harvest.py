"""Harvest executable examples and bind their input shapes to upstream test material."""
import json
import tarfile

from frf.scales.repo import Material, Repo
from frf.source.repo_harvest import (bind_inputs, command_index, fixture_archive,
                                    fixture_dependencies, harvest_corpus, harvest_files)


def test_mentions_in_installers_assignments_and_file_commands_are_not_invocations(tmp_path):
    (tmp_path / 'README.md').write_text(
        '```sh\nbrew install tool\nbrew uninstall tool\nsudo mv tool /usr/bin/\n'
        'rm tool\nREPO=owner/tool\necho tool\n'
        'tool validate inputs/a.json\nexec tool format inputs/b.json\n```\n')
    found = harvest_files(str(tmp_path), ('tool',))
    assert [item.argv for item in found] == [
        ('tool', 'validate', 'inputs/a.json'), ('exec', 'tool', 'format', 'inputs/b.json')]
    assert [item.line for item in found] == [8, 9]
    assert command_index(['node', 'dist/cli.js', 'input.json'], ('node', 'cli.js')) == 1
    assert command_index(['python3', '-m', 'tool', 'input.json'], ('python3', 'tool')) == 2
    assert command_index(['python3', 'unrelated.py'], ('python3', 'tool')) is None
    assert command_index(['poetry', 'run', 'tool', 'input.json'], ('tool',)) == 2


def test_missing_documented_input_uses_real_corpus_without_changing_output_path(tmp_path):
    inputs = ['fixtures/b.json', 'fixtures/a.json', 'fixtures/a.yaml']
    for relative in inputs:
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text('{}')
    argv = ['tool', 'convert', 'input.json', 'output.json', '--pretty']
    variants = bind_inputs(str(tmp_path), argv, inputs)
    assert variants == [['tool', 'convert', name, 'output.json', '--pretty']
                        for name in ['fixtures/a.json', 'fixtures/b.json']]
    assert bind_inputs(str(tmp_path), ['tool', '--input=input.json'], inputs)[0] == [
        'tool', '--input=fixtures/a.json']
    assert bind_inputs(str(tmp_path), ['tool', '../input.json'], inputs) == []


def test_fixture_budget_is_shared_across_upstream_test_directories(tmp_path):
    for family in ('a', 'b', 'c'):
        room = tmp_path / family / 'testdata'
        room.mkdir(parents=True)
        for index in range(10):
            (room / (str(index) + '.json')).write_text('{}')
    assert harvest_corpus(str(tmp_path), max_files=6) == [
        family + '/testdata/' + str(index) + '.json'
        for index in range(2) for family in ('a', 'b', 'c')]


def test_binding_respects_compound_types_and_command_context(tmp_path):
    inputs = ['arazzo/testdata/simple.arazzo.yaml', 'openapi/testdata/simple.openapi.yaml',
              'swagger/testdata/petstore.swagger.yaml', 'swagger/testdata/petstore.expected.openapi.yaml']
    for relative in inputs:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('data')
    bound = bind_inputs(str(tmp_path), ['{PROGRAM}', 'spec', 'inline', 'spec.yaml'], inputs,
                        context=('openapi', 'README.md'))
    assert bound[0][-1] == 'openapi/testdata/simple.openapi.yaml'
    bound = bind_inputs(str(tmp_path), ['{PROGRAM}', 'swagger', 'upgrade', 'api.swagger.yaml'], inputs)
    assert [item[-1] for item in bound] == ['swagger/testdata/petstore.swagger.yaml']


def test_corpus_prioritizes_inputs_and_shares_budget_with_nested_workloads(tmp_path):
    root = tmp_path / 'testdata'
    root.mkdir()
    for name in ('a.expected.yaml', 'b.invalid.yaml', 'c.input.yaml'):
        (root / name).write_text('data')
    (root / 'bundle').mkdir()
    (root / 'bundle' / 'input.yaml').write_text('data')
    assert harvest_corpus(str(tmp_path), max_files=2) == ['testdata/c.input.yaml', 'testdata/bundle/input.yaml']


def test_local_document_dependencies_are_staged_transitively_and_stay_inside_repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'schema.yaml').write_text("first: {$ref: 'parts/child.json#/x'}\nremote: {$ref: 'https://example.invalid/a.yaml'}\n")
    (root / 'parts').mkdir()
    (root / 'parts/child.json').write_text(json.dumps({'$ref': '../shared.yaml'}))
    (root / 'shared.yaml').write_text("cycle: {$ref: 'schema.yaml'}\noutside: {$ref: '../private.yaml'}\n")
    (tmp_path / 'private.yaml').write_text('private: true')
    (root / 'linked.yaml').symlink_to(tmp_path / 'private.yaml')
    paths = fixture_dependencies(str(root), ['schema.yaml', 'linked.yaml'])
    assert paths == ['parts/child.json', 'schema.yaml', 'shared.yaml']
    name = fixture_archive(str(root), paths + ['linked.yaml'], str(tmp_path / 'packed'))
    with tarfile.open(tmp_path / 'packed' / name) as archive:
        assert sorted(archive.getnames()) == paths


def test_large_upstream_inputs_are_sampled_with_a_total_byte_budget(tmp_path):
    root = tmp_path / 'testdata'
    root.mkdir()
    for index in range(70):
        (root / ('a-%02d.json' % index)).write_text('{}')
    (root / 'z-large.json').write_text(' ' * 300000 + '{}')
    chosen = harvest_corpus(str(tmp_path), max_files=4)
    assert 'testdata/z-large.json' in chosen
    assert 'testdata/a-00.json' in chosen
    bounded = harvest_corpus(str(tmp_path), max_files=4, max_total_bytes=100)
    assert sum((tmp_path / name).stat().st_size for name in bounded) <= 100


def test_repo_harvest_preserves_subcommands_inputs_and_provenance(tmp_path):
    (tmp_path / 'go.mod').write_text('module example.invalid/tool\n')
    (tmp_path / 'README.md').write_text(
        '```sh\nbrew install tool\ntool convert spec.json result.json\n'
        'tool convert spec.json result.json\ntool --help\n```\n')
    room = tmp_path / 'testdata'
    room.mkdir()
    for index in range(12):
        (room / ('input-%02d.json' % index)).write_text(json.dumps({'id': index}))
    repo = Repo()
    repo._material = Material(identity='test://tool', language='go', root=str(tmp_path),
                              invoke=['{ROOT}/program'])
    scenarios = repo._harvest_repository_workload()
    work = [scenario for scenario in scenarios if scenario.steps[0].argv[1] == 'convert']
    assert len(work) == 12
    assert len(scenarios) == 13
    assert all(s.steps[0].argv[-1] == 'result.json' for s in work)
    assert all(s.steps[0].argv[2].startswith('testdata/') for s in work)
    with tarfile.open(tmp_path / '.frf-fixtures' / work[0].fixture) as archive:
        assert set(archive.getnames()) == {s.steps[0].argv[2] for s in work}
    provenance = json.loads((tmp_path / '.frf-workload-provenance.json').read_text())
    assert len(provenance) == len(scenarios)
    assert provenance[0]['source'] == 'README.md' and provenance[0]['line'] == 3
    assert provenance[0]['documented_argv'] == ['tool', 'convert', 'spec.json', 'result.json']
