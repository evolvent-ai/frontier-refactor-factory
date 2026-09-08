import shlex
from types import SimpleNamespace

import pytest

from frf.observe.reference_image import reference_context, write_reference_image


def test_absent_reference_runtime_declaration_uses_current_image(tmp_path):
    assert reference_context(tmp_path) == (str(tmp_path/'reference'), '/', {})


def test_reference_runtime_paths_cannot_be_replaced_by_task_data(tmp_path):
    (tmp_path/'reference-runtime.json').write_text('{"root":"/app"}')
    with pytest.raises(ValueError, match='declaration'):
        reference_context(tmp_path)


def test_same_language_does_not_add_an_unnecessary_runtime(tmp_path):
    write_reference_image(tmp_path, SimpleNamespace(language='go', target_language='go'))
    assert not list(tmp_path.iterdir())


def test_cross_reference_recipe_keeps_source_toolchain_below_separate_root(tmp_path):
    (tmp_path/'tests').mkdir()
    spec = SimpleNamespace(language='go', target_language='cpp', scale='repo')
    write_reference_image(tmp_path,spec,install=[['go','build','-o','{ROOT}/program','.']])
    recipe = (tmp_path/'tests/Dockerfile').read_text()
    assert 'AS source-runtime' in recipe
    command = next(line[4:] for line in recipe.splitlines() if line.startswith('RUN go build'))
    assert shlex.split(command) == ['go', 'build', '-o', '/app/program', '.']
    assert 'FROM ${SOLVER_IMAGE}' in recipe
    assert 'COPY --from=source-runtime --chown=0:0 / /reference-runtime/' in recipe
    assert 'COPY --from=source-runtime /usr /usr' not in recipe
