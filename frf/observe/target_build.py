"""Trusted target-language builds inside an existing confined candidate root."""
import hashlib
import os
import shutil
import stat
import tomllib
from pathlib import Path


TARGETS = {
    'rust': {'tool': 'cargo', 'entry': 'src/main.rs', 'manifest': 'Cargo.toml'},
    'go': {'tool': 'go', 'entry': 'main.go', 'manifest': 'go.mod'},
    'cpp': {'tool': 'c++', 'entry': 'main.cpp', 'manifest': None},
}


class TargetBuildError(RuntimeError):
    pass


def target_plan(language):
    language = str(language).lower()
    if language not in TARGETS:
        raise ValueError('target language lacks a verified build adapter: ' + language)
    return dict(TARGETS[language], language=language)


def _validate_inputs(directory):
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            if path.is_symlink() and not path.resolve().is_relative_to(directory.resolve()):
                raise TargetBuildError('target build input links outside the implementation directory')
            if not path.is_symlink() and not (path.is_file() or path.is_dir()):
                raise TargetBuildError('unsupported target build input type')


def _own(directory, uid):
    for parent, directories, files in os.walk(directory, followlinks=False):
        os.chown(parent, uid, uid)
        for name in directories + files:
            os.chown(Path(parent) / name, uid, uid, follow_symlinks=False)


def _digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def build_target(root, language, *, reference_binaries=(), timeout=600):
    """Build only implementation/, stop all build processes, and seal the selected ELF.

    Runtime execution restrictions are a separate step. This function never treats a
    successful compiler invocation as proof of functional correctness or non-delegation.
    """
    plan = target_plan(language)
    app = root.path / 'app'
    implementation = app / 'implementation'
    if implementation.is_symlink() or not implementation.is_dir():
        raise TargetBuildError('the target implementation directory is missing or linked')
    _validate_inputs(implementation)
    for name in (plan['entry'], plan['manifest']):
        if name and not (implementation / name).is_file():
            raise TargetBuildError('required target build input is missing: ' + name)
    compiler = shutil.which(plan['tool'])
    if plan['language'] == 'rust':
        rustup_home = Path(os.environ.get('RUSTUP_HOME', '/usr/local/rustup'))
        settings = rustup_home / 'settings.toml'
        if not settings.is_file():
            raise TargetBuildError('the image does not identify its installed Rust toolchain')
        installed = tomllib.loads(settings.read_text()).get('default_toolchain', '')
        if not installed or '/' in installed or installed in ('.', '..'):
            raise TargetBuildError('invalid installed Rust toolchain identity')
        compiler = str(rustup_home / 'toolchains' / installed / 'bin/cargo')
    if not compiler:
        raise TargetBuildError('target compiler is unavailable: ' + plan['tool'])
    compiler_info = os.stat(compiler)
    if compiler_info.st_uid != 0:
        raise TargetBuildError('target compiler is not immutable trusted runtime content')
    confined_compiler = root.path / compiler.lstrip('/')
    if not confined_compiler.exists():
        raise TargetBuildError('target compiler is absent from the confined runtime')
    confined_info = confined_compiler.stat()
    if confined_info.st_uid != 0 or confined_info.st_mode & 0o022:
        raise TargetBuildError('target compiler was not sealed in the confined runtime')

    original = root.room / 'workspace-before-target-build'
    work = root.path / '.target-build'
    sealed = root.path / '.target'
    if original.exists() or work.exists() or sealed.exists():
        raise TargetBuildError('target build directories must be fresh')
    root.stop()
    if root.execution_audit is not None or root.execution_logs:
        raise TargetBuildError('target build requires a fresh execution-audit context')
    app.rename(original)
    try:
        shutil.copytree(original / 'implementation', app, symlinks=True)
        _own(app, root.uid)
        work.mkdir(mode=0o755)
        os.chown(work, root.uid, root.uid)
        environment = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}
        preparation = []
        if plan['language'] == 'rust':
            cargo_home = root.path / 'home/subject/.cargo'
            cargo_home.mkdir()
            os.chown(cargo_home, root.uid, root.uid)
            registry = root.path / 'usr/local/cargo/registry'
            if registry.is_dir():
                (cargo_home / 'registry').symlink_to('/usr/local/cargo/registry')
            environment.update(CARGO_HOME='/home/subject/.cargo',
                               RUSTUP_HOME=os.environ.get('RUSTUP_HOME', '/usr/local/rustup'),
                               RUSTC=str(Path(compiler).with_name('rustc')),
                               RUSTDOC=str(Path(compiler).with_name('rustdoc')),
                               LD_LIBRARY_PATH=str(Path(compiler).parent.parent / 'lib'),
                               CARGO_ENCODED_RUSTFLAGS='--sysroot\x1f' + str(Path(compiler).parent.parent)
                               + '\x1f-C\x1flink-arg=-fuse-ld=bfd',
                               RUSTC_WRAPPER='', RUSTC_WORKSPACE_WRAPPER='')
            argv = [compiler, 'build', '--offline', '--release', '--manifest-path', '/app/Cargo.toml',
                    '--target-dir', '/.target-build', '--bin', 'submission']
            output = work / 'release/submission'
        elif plan['language'] == 'go':
            environment.update(GOTOOLCHAIN='local', GOWORK='off', GOPROXY='off', GOSUMDB='off',
                               GOFLAGS='', GOENV='off', GOCACHE='/.target-build/cache',
                               GOROOT=str(Path(compiler).resolve().parent.parent), GOTELEMETRY='off')
            argv = [compiler, 'build', '-o', '/.target-build/program', '/app']
            output = work / 'program'
        else:
            sources = sorted(path for path in app.rglob('*') if path.suffix in ('.cpp', '.cc', '.cxx')
                             and not any(part in ('tests', 'test') for part in path.relative_to(app).parts))
            if not sources:
                raise TargetBuildError('the target project contains no C++ translation units')
            objects = []
            for index, source in enumerate(sorted(app.rglob('*.c'))):
                object_file = '/.target-build/c-%d.o' % index
                preparation.append([compiler, '-x', 'c', '-std=c11', '-O3', '-I/app', '-c',
                                    '/app/' + source.relative_to(app).as_posix(), '-o', object_file])
                objects.append(object_file)
            argv = [compiler, '-std=c++20', '-O3', '-pthread', '-I/app',
                    *('/app/' + path.relative_to(app).as_posix() for path in sources),
                    *objects,
                    '-o', '/.target-build/program']
            output = work / 'program'
        try:
            root.audit_execution()
            for command in preparation:
                prepared = root.run(command, cwd='/.target-build', environment=environment, timeout=timeout)
                root.stop()
                if prepared.returncode != 0:
                    raise TargetBuildError('target adapter build failed: ' + prepared.stderr[-1500:])
            completed = root.run(argv, cwd='/app' if plan['language'] == 'go' else '/.target-build',
                                 environment=environment, timeout=timeout)
        finally:
            root.stop()
        execution = root.execution_report()
        if not execution['complete'] or execution['denied']:
            raise TargetBuildError('target compiler execution audit did not complete')
        executable = str(confined_compiler.resolve()).removeprefix(str(root.path))
        if not any(event['path'] == executable for event in execution['events']):
            raise TargetBuildError('the trusted target compiler was not executed')
        if completed.returncode != 0:
            raise TargetBuildError('target build failed: ' + completed.stderr[-1500:])
        if output.is_symlink() or not output.is_file():
            raise TargetBuildError('target build did not produce a regular executable')
        with output.open('rb') as handle:
            if handle.read(4) != b'\x7fELF':
                raise TargetBuildError('target build did not produce a native ELF executable')
        digest = _digest(output)
        if any(Path(path).is_file() and _digest(Path(path)) == digest for path in reference_binaries):
            raise TargetBuildError('target output is a copied reference binary')
        sealed.mkdir(mode=0o755)
        artifact = sealed / 'program'
        shutil.copyfile(output, artifact)
        artifact.chmod(0o555)
        info = artifact.stat()
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode):
            raise TargetBuildError('target artifact could not be sealed')
        return {'language': plan['language'], 'build_argv': argv, 'sha256': digest,
                'compiler_sha256': _digest(confined_compiler),
                'build_execution': execution,
                'preparation_argv': preparation,
                'entry_argv': ['/.target/program'], 'build_exit_code': completed.returncode,
                'runtime_enforced': False}
    finally:
        root.stop()
        root.execution_audit = None
        root.execution_logs.clear()
        if app.exists():
            shutil.rmtree(app)
        original.rename(app)


def standalone_source():
    return Path(__file__).read_text()
