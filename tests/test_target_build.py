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
