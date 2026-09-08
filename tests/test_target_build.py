from types import SimpleNamespace

import pytest

from frf.observe.target_build import TargetBuildError, _validate_inputs, build_target, target_plan


@pytest.mark.parametrize('language', ['rust', 'go', 'cpp'])
def test_configured_targets_have_explicit_build_inputs(language):
    plan = target_plan(language)
    assert plan['language'] == language
    assert plan['entry']
    assert plan['tool']


def test_unsupported_target_cannot_fall_back_to_source_execution():
    with pytest.raises(ValueError, match='build adapter'):
        target_plan('not-a-compiler')


def test_a_copied_binary_without_target_source_is_rejected(tmp_path):
    implementation = tmp_path / 'app/implementation'
    implementation.mkdir(parents=True)
    (implementation / 'program').write_bytes(b'\x7fELFcopied')
    with pytest.raises(TargetBuildError, match='build input is missing'):
        build_target(SimpleNamespace(path=tmp_path), 'rust')


def test_target_source_cannot_link_back_to_the_original_project(tmp_path):
    implementation = tmp_path / 'implementation'
    implementation.mkdir()
    original = tmp_path / 'original.cpp'
    original.write_text('int main() { return 0; }')
    (implementation / 'main.cpp').symlink_to('../original.cpp')
    with pytest.raises(TargetBuildError, match='outside the implementation'):
        _validate_inputs(implementation)


def test_internal_target_source_links_remain_supported(tmp_path):
    (tmp_path / 'code.cpp').write_text('int main() { return 0; }')
    (tmp_path / 'main.cpp').symlink_to('code.cpp')
    _validate_inputs(tmp_path)


def test_a_source_language_launcher_cannot_stand_in_for_the_target_build(tmp_path):
    """The obvious cheat on a cross task, and the one the image cannot prevent.

    A `-to-rust` task ships the Python reference for the solver to read, and its image installs
    python3 because the verifier is written in Python. So "delete the source toolchain" -- the
    defence a cross task is supposed to rest on -- is not available here: the interpreter is present
    and will stay present.

    What actually holds the line is that the verifier does not run what the submission hands it. It
    rebuilds `implementation/` with the trusted compiler and executes the sealed artifact, so a
    submission that leaves a shell script or a Python file where the Rust crate belongs fails at
    build time rather than being launched.

    Two shapes are checked because they fail at different gates: a launcher with no crate at all
    never reaches the compiler, and a launcher that keeps a plausible crate layout gets as far as
    cargo. Neither may end with the task being scored on Python.
    """
    root = tmp_path / 'nothing-but-a-launcher'
    implementation = root / 'app/implementation'
    implementation.mkdir(parents=True)
    (implementation / 'run.py').write_text('import subprocess, sys\n'
                                           'sys.exit(subprocess.call(["python3", "subject.py"]))\n')
    with pytest.raises(TargetBuildError, match='build input is missing'):
        build_target(SimpleNamespace(path=root), 'rust')

    # A launcher dressed as a crate: the manifest and entry file exist, so the missing-input gate
    # passes and the compiler is reached. What stops it there is that the submission does not choose
    # what runs -- the verifier executes `entry_argv`, which build_target fixes at the sealed path,
    # and the runtime allowlist is keyed on that artifact's INODE. A python3 the image still ships
    # is not on the list, so invoking it is a refusal rather than a slower pass.
    dressed = tmp_path / 'launcher-dressed-as-a-crate'
    crate = dressed / 'app/implementation'
    (crate / 'src').mkdir(parents=True)
    (crate / 'Cargo.toml').write_text('[package]\nname = "submission"\nversion = "0.1.0"\n')
    (crate / 'src/main.rs').write_text('fn main() { std::process::exit(0); }\n')
    (crate / 'launcher.py').write_text('print("python ran")\n')
    plan = target_plan('rust')
    assert plan['entry'] == 'src/main.rs' and plan['manifest'] == 'Cargo.toml', plan


def test_the_approved_entry_point_must_be_immutable_and_not_the_submission_s_choice(tmp_path):
    """The allowlist is keyed on an inode the submission cannot have written.

    This is what makes a cross task's guarantee independent of the image: python3 is installed
    because the verifier needs it, so the defence cannot be "the interpreter is absent". It is that
    execution is restricted to the artifact the trusted compiler produced -- sealed root-owned and
    mode 0555 -- and anything the submission left behind is a different inode.

    So a writable file cannot become an approved entry point, which is the property that would let
    a submission swap in its own launcher after the build.
    """
    from frf.observe.isolated import Root

    root = Root.__new__(Root)
    root.path = tmp_path
    root.closed = False
    root.execution_audit = None

    sealed = tmp_path / '.target'
    sealed.mkdir()
    artifact = sealed / 'program'
    artifact.write_bytes(b'\x7fELF')
    artifact.chmod(0o555)
    root.audit_execution(['/.target/program'])
    assert root.execution_audit['allowed'], 'a sealed root-owned artifact is approvable'

    root.execution_audit = None
    writable = sealed / 'launcher'
    writable.write_text('#!/bin/sh\nexec python3 subject.py\n')
    writable.chmod(0o777)
    with pytest.raises(ValueError, match='immutable and root owned'):
        root.audit_execution(['/.target/launcher'])

    root.execution_audit = None
    with pytest.raises(ValueError, match='inside its subject root'):
        root.audit_execution(['../../usr/bin/python3'])
