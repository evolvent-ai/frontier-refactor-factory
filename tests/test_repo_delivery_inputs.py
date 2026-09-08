"""Repository build inputs survive packaging without replacing the evaluator Dockerfile."""
from types import SimpleNamespace

from frf.core.scale import Spec
from frf.scales.repo import Material, Repo


def test_writer_preserves_upstream_dockerfiles_docs_and_fixtures(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for directory in ('docs', 'fixtures', 'nested'):
        (source / directory).mkdir()
    for name in ('README.md', 'docs/help.md', 'fixtures/data', 'nested/Dockerfile'):
        (source / name).write_text('upstream input\n')
    (source / 'Dockerfile').write_text('FROM scratch\n')
    (source / '.dockerignore').write_text('docs\nfixtures\n')
    task = tmp_path / 'task'
    (task / 'environment').mkdir(parents=True)
    (task / 'environment/Dockerfile').write_text('FROM golang:1.26-bookworm\nUSER nobody\n')
    repo = Repo()
    repo._material = Material(identity='test://repo', language='go', root=str(source), invoke=['./program'])
    repo._spec = Spec('repo-opt', 'repo', 'go', 'Process data', build=[['go', 'build', '.']], invoke=['./program'])
    corpus = SimpleNamespace(expectations={}, scenarios=[], timed=[], timed_expectations={})
    repo.write_tests(str(task), corpus)
    dockerfile = (task / 'environment/Dockerfile').read_text()
    assert dockerfile.startswith('FROM golang:1.26-bookworm')
    assert dockerfile.index('COPY .upstream-Dockerfile /app/Dockerfile') < dockerfile.index('RUN go build .')
    assert (task / 'environment/.upstream-Dockerfile').read_text() == 'FROM scratch\n'
    assert (task / 'tests/reference/Dockerfile').read_text() == 'FROM scratch\n'
    assert (task / 'environment/nested/Dockerfile').read_text() == 'upstream input\n'
    ignores = (task / 'environment/.dockerignore').read_text().splitlines()
    assert not {'docs', 'fixtures', '*.md', '*.rst', '**/docs', '**/fixtures'} & set(ignores)
    assert (task / 'environment/README.md').is_file()


def test_missing_image_inventory_refuses_before_verifier_execution(tmp_path):
    from frf.core.sandbox import Result
    from frf.observe.in_image import drive
    (tmp_path / 'environment').mkdir()
    (tmp_path / 'environment/Dockerfile').write_text('FROM image\n')
    (tmp_path / 'environment/README.md').write_text('documented input\n')
    commands = []
    def run(argv, **kwargs):
        command = argv[-1]
        commands.append(command)
        if command.startswith('docker image inspect '):
            return Result(0, 'sha256:' + 'a' * 64, '')
        if '--entrypoint python3' in command:
            assert '/app/README.md' in command
            return Result(1, '{"missing":["/app/README.md"]}', '')
        return Result(0, '', '')
    backend = SimpleNamespace(name='remote', push=lambda *a, **kw: None, run=run)
    result = drive(str(tmp_path), backend=backend)
    assert not result['ok']
    assert result['stage'] == 'workspace-inventory'
    assert '/app/README.md' in result['detail']
    assert not any('tests/verify.py' in command for command in commands)
    assert result['cleanup_ok']
