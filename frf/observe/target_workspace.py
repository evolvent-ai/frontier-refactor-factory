"""Target-language scaffolding with no implementation or optimized reference supplied."""
import json
from pathlib import Path

from .call import shims
from .target_build import target_plan


def _template(language):
    return Path(shims.__file__).with_name(shims.load(language).template).read_text()


def write_target_workspace(directory, language, *, call_interface=True):
    plan = target_plan(language)
    root = Path(directory)
    implementation = root / 'implementation'
    implementation.mkdir()
    if language == 'rust':
        (implementation / 'src').mkdir()
        (implementation / 'Cargo.toml').write_text(
            '[package]\nname = "submission"\nversion = "0.0.0"\nedition = "2021"\n')
        if call_interface:
            (implementation / 'src/main.rs').write_text(_template('rust'))
            (implementation / 'src/subject.rs').write_text(
                'pub fn entry(_args: &crate::Json) -> Result<crate::Json, String> {\n'
                '    Err("target implementation is not provided".to_string())\n}\n')
        else:
            (implementation / 'src/main.rs').write_text('fn main() { std::process::exit(1); }\n')
        command = 'cargo build --offline --release --manifest-path implementation/Cargo.toml --target-dir /app/build --bin submission'
        run = 'exec /app/build/release/submission "$@"'
    elif language == 'go':
        (implementation / 'go.mod').write_text('module benchmark.submission\ngo 1.22\n')
        if call_interface:
            (implementation / 'main.go').write_text(_template('go'))
            (implementation / 'subject.go').write_text(
                'package main\nimport "fmt"\n'
                'func Entry(args []interface{}) (interface{}, error) {\n'
                '    return nil, fmt.Errorf("target implementation is not provided")\n}\n')
        else:
            (implementation / 'main.go').write_text('package main\nimport "os"\nfunc main() { os.Exit(1) }\n')
        command = 'cd /app/implementation && GOTOOLCHAIN=local GOPROXY=off go build -o /app/build/submission .'
        run = 'exec /app/build/submission "$@"'
    else:
        if call_interface:
            (implementation / 'serve.c').write_text(_template('cpp'))
            (implementation / 'main.cpp').write_text(
                '#include <cstddef>\nextern "C" {\nchar *entry_error = nullptr;\n'
                'const char *entry(const char *) {\n'
                '    entry_error = const_cast<char *>("target implementation is not provided");\n'
                '    return nullptr;\n}\n}\n')
            prefix = 'c++ -x c -std=c11 -O3 -c implementation/serve.c -o build/serve.o\n'
            objects = 'build/serve.o '
        else:
            (implementation / 'main.cpp').write_text('int main() { return 1; }\n')
            prefix, objects = '', ''
        command = prefix + 'c++ -std=c++20 -O3 -pthread -Iimplementation implementation/*.cpp ' + objects + '-o build/submission'
        run = 'exec /app/build/submission "$@"'
    (root / 'build.sh').write_text('#!/bin/sh\nset -eu\ncd /app\nmkdir -p build\n' + command + '\n')
    (root / 'run.sh').write_text('#!/bin/sh\nset -eu\n/app/build.sh\n' + run + '\n')
    for name in ('build.sh', 'run.sh'):
        (root / name).chmod(0o755)
    context = {'build_commands': ['/app/build.sh'], 'run_command': '/app/run.sh',
               'build_directory': '/app', 'entry_source': '/app/implementation/' + plan['entry'],
               'implementation_directory': '/app/implementation', 'launcher_builds': True,
               'target_language': language,
               'build_contract': 'The evaluator builds the target sources in /app/implementation with its trusted '
                                 'offline toolchain and executes that product directly. /app/run.sh and /app/build.sh '
                                 'are development entrypoints; replacing them does not replace the graded program. '
                                 'Put implementation sources and build inputs under /app/implementation.'}
    (root / 'task-interface.json').write_text(json.dumps(context, indent=2))
    return context
