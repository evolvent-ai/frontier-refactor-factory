"""Build a verifier-only source runtime beside an independent solver runtime."""
import json
import shlex
from pathlib import Path

from ..core.harbor import dockerfile_for


def write_reference_image(task_dir, spec, *, install=()):
    source, target = spec.language.lower(), (spec.target_language or spec.language).lower()
    if source == target:
        return
    tests = Path(task_dir) / 'tests'
    recipe = dockerfile_for(source, source).splitlines()
    last_from = max(index for index, line in enumerate(recipe) if line.startswith('FROM '))
    recipe[last_from] += ' AS source-runtime'
    recipe = ['ARG SOLVER_IMAGE'] + recipe
    recipe += ['USER root', 'COPY reference/ /app/', 'WORKDIR /app']
    for command in install:
        rendered = shlex.join(map(str, command)) if isinstance(command, (list, tuple)) else str(command)
        if rendered:
            recipe.append('RUN ' + rendered.replace('{ROOT}', '/app'))
    declaration = tests / 'reference/task-interface.json'
    if spec.scale != 'repo' and declaration.is_file():
        context = json.loads(declaration.read_text())
        commands = context.get('build_commands') or []
        for command in commands:
            recipe.append('RUN ' + command)
        argv = context.get('runtime_argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
            raise ValueError('reference build is missing its declared runtime invocation')
        argv = ['/app/' + value[2:] if value.startswith('./') else value for value in argv]
        launcher = '#!/bin/sh\nunset ENV BASH_ENV\nexec ' + shlex.join(argv) + ' "$@"\n'
        write = 'from pathlib import Path; p=Path("/app/run.sh"); p.write_text(' + repr(launcher) + '); p.chmod(0o755)'
        recipe.append('RUN python3 -c ' + shlex.quote(write))
    environment_keys = ['PATH', 'GOROOT', 'GOPATH', 'GOTOOLCHAIN', 'CARGO_HOME', 'RUSTUP_HOME',
                        'JAVA_HOME', 'LD_LIBRARY_PATH', 'PYTHONPATH']
    capture = ('import json,os; json.dump({k:os.environ[k] for k in ' + repr(environment_keys)
               + ' if k in os.environ},open("/runtime-environment.json","w"))')
    recipe += ['RUN python3 -c ' + shlex.quote(capture), 'FROM ${SOLVER_IMAGE}', 'USER root',
               'COPY --from=source-runtime --chown=0:0 / /reference-runtime/', 'COPY . /tests/', 'WORKDIR /app', '']
    (tests / 'Dockerfile').write_text('\n'.join(recipe))
    (tests / '.dockerignore').write_text('.git\n.hg\n__pycache__\n')
    (tests / 'reference-runtime.json').write_text(json.dumps({
        'root': '/reference-runtime', 'workspace': '/reference-runtime/app',
        'environment': '/reference-runtime/runtime-environment.json'}, indent=2))


def reference_context(tests):
    """Read trusted emitted runtime configuration; never accept candidate paths."""
    tests = Path(tests)
    declaration = tests / 'reference-runtime.json'
    if not declaration.exists():
        return str(tests / 'reference'), '/', {}
    data = json.loads(declaration.read_text())
    expected = {'root': '/reference-runtime', 'workspace': '/reference-runtime/app',
                'environment': '/reference-runtime/runtime-environment.json'}
    if data != expected:
        raise ValueError('invalid reference runtime declaration')
    base = Path(data['root'])
    workspace = Path(data['workspace'])
    config = Path(data['environment'])
    if base.is_symlink() or workspace.is_symlink() or config.is_symlink():
        raise ValueError('reference runtime paths must not be linked')
    if not (workspace / 'run.sh').is_file() or not config.is_file():
        raise RuntimeError('the verifier image does not contain the declared reference runtime')
    environment = json.loads(config.read_text())
    if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                               for key, value in environment.items()):
        raise ValueError('invalid reference runtime environment')
    return str(workspace), str(base), environment


def standalone_source():
    import inspect
    return inspect.getsource(reference_context)
