"""Source observation must use its declared image; image replay stays on outer E2B."""
from types import SimpleNamespace
from pathlib import Path
import pytest
from frf.core.containers import RemoteImage
from frf.core.sandbox import Result, SandboxError
from frf.observe.reference_backend import source_runtime
from frf.observe.replay import ImageReplay


def test_runtime_routes_transfers_and_commands_without_host_docker(tmp_path):
    calls=[]
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:3] == ['docker','image','inspect']:
            return Result(0, 'sha256:' + 'a'*64, '')
        if argv[:3] == ['docker','exec','--user']:
            return Result(0, 'python3\ngo\nrustc\ncargo\ngcc\n', '')
        return Result(0, '', '')
    def push(local, remote, **kwargs):
        calls.append(('push', remote))
        if Path(local, 'Dockerfile').exists():
            assert Path(local, 'Dockerfile').read_text() == 'FROM pinned\n'
    control=SimpleNamespace(name='remote',run=run,push=push,
        pull=lambda remote, local: calls.append(('pull', remote)))
    runtime=RemoteImage(control,'FROM pinned\n')
    runtime.push(str(tmp_path),'/tmp/source with spaces')
    runtime.run(['python3','-c','print(1)'],workdir='/tmp/source with spaces',
                env={'INPUT':'literal $x'},timeout=12)
    runtime.pull('/tmp/source with spaces',str(tmp_path))
    command, options = next((argv, kw) for argv, kw in calls
                           if isinstance(argv,list) and 'timeout' in argv)
    assert command[-5:] == ['--kill-after=5s','12s','python3','-c','print(1)']
    assert options['timeout'] > 12
    env_values = [command[i + 1] for i, value in enumerate(command) if value == '--env']
    assert 'INPUT=literal $x' in env_values
    assert 'PATH=/usr/local/go/bin:/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin' in env_values
    assert sum(isinstance(a,list) and a[:2] == ['docker','build'] for a,_ in calls) == 1
    assert ImageReplay(runtime).backend is control
    runtime.close()
    assert calls[-2][0] == ['docker','rm','-f',runtime.container]


def test_failed_image_build_cannot_fall_back_to_template_runtime():
    commands=[]
    def run(argv, **kwargs):
        commands.append(argv)
        return Result(1,'','build failed')
    control=SimpleNamespace(name='remote',run=run,push=lambda *a,**k:None)
    runtime=RemoteImage(control,'FROM pinned\n')
    with pytest.raises(SandboxError,match='build failed'):
        runtime.run(['python3','--version'])
    assert not any(cmd[:2] == ['docker','exec'] or cmd[0] == 'python3' for cmd in commands)


def test_source_runtime_uses_source_language_even_for_cross_task():
    recipes=[]
    backend=SimpleNamespace(runtime=lambda recipe: recipes.append(recipe) or object())
    source_runtime(backend,'python')
    assert 'FROM python:3.12.8-slim-bookworm@sha256:' in recipes[0]
    assert 'FROM rust:' not in recipes[0]


@pytest.mark.parametrize('bad',['relative','/','/tmp/../etc'])
def test_transfer_paths_cannot_escape_or_replace_container_root(bad):
    with pytest.raises(ValueError):
        RemoteImage._path(bad)
