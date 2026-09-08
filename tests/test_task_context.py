from frf.core.scale import Spec
from frf.core.task_context import collect


def test_collects_only_solver_visible_paths(tmp_path):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "src").mkdir()
    (environment / "src" / "main.go").write_text("package main\n")
    (environment / ".git").mkdir()
    (environment / ".git" / "config").write_text("internal")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "expectations.json").write_text("secret")
    spec = Spec(name="x-opt", scale="repo", language="go", description="x",
                environment={"scope_guidance": "repo"})
    enriched = collect(str(tmp_path), spec)
    context = enriched.environment["instruction_context"]
    assert "/app/src/main.go" in context["workspace_paths"]
    assert not any(".git" in path or "expectations" in path for path in context["workspace_paths"])
    assert enriched.environment["scope_guidance"] == "repo"
    assert "instruction_context" not in spec.environment


def test_metadata_cannot_displace_entrypoint_and_deep_source_files(tmp_path):
    environment = tmp_path / 'environment'
    for directory in ('.github/workflows', 'cmd/tool', 'src/project/nested/parser'):
        (environment / directory).mkdir(parents=True)
    for index in range(100):
        (environment / '.github/workflows' / ('%03d.yml' % index)).write_text('metadata')
    (environment / 'README.md').write_text('Project documentation')
    (environment / 'go.mod').write_text('module example\n')
    (environment / 'cmd/tool/main.go').write_text('package main\n')
    (environment / 'src/project/nested/parser/parse.go').write_text('package parser\n')
    spec = Spec(name='project-opt', scale='repo', language='go', description='Parse documents')
    paths = collect(str(tmp_path), spec).environment['instruction_context']['workspace_paths']
    assert '/app/cmd/tool/main.go' in paths[:20]
    assert '/app/src/project/nested/parser/parse.go' in paths[:20]
    assert '/app/README.md' in paths[:20]
    assert not any('/.github/' in path for path in paths)


def test_source_inventory_spreads_across_modules(tmp_path):
    from frf.core.task_context import workspace_paths
    for module in ('a', 'b', 'c'):
        (tmp_path / module).mkdir()
        for index in range(90):
            (tmp_path / module / ('%03d.go' % index)).write_text('package module\n')
    paths = workspace_paths(tmp_path)
    assert len(paths) == 80
    assert {path.split('/')[2] for path in paths[:3]} == {'a', 'b', 'c'}


def test_build_entrypoint_precedes_many_shallower_library_directories(tmp_path):
    import json
    from frf.core.task_context import workspace_paths
    for index in range(50):
        module = tmp_path / ('library%02d' % index)
        module.mkdir()
        (module / 'library.go').write_text('package library\n')
    (tmp_path / 'cmd/tool').mkdir(parents=True)
    (tmp_path / 'cmd/tool/main.go').write_text('package main\n')
    (tmp_path / 'task-interface.json').write_text(json.dumps({
        'preparation_commands': ['go build -o ./program ./cmd/tool']}))
    paths = workspace_paths(tmp_path)
    assert '/app/cmd/tool/main.go' in paths[:3]
