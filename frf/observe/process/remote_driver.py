"""Remote process observation with bounded pipes and per-step execution deadlines."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from ..transport import bounded_run
from ..isolated import extract_fixture, workspace_directory
from .snapshot import tree_lines


def execute(request):
    answers = {}
    root = Path(request['root'])
    uid = None
    program = request['program']
    if os.geteuid() == 0 and program and program[0] == 'setpriv' and '--reuid' in program:
        import pwd
        user = program[program.index('--reuid') + 1]
        uid = int(user) if user.isdigit() else pwd.getpwnam(user).pw_uid
    for number, scenario in enumerate(request['scenarios']):
        workspace = root / ('workspace-%03d' % number)
        workspace.mkdir(mode=0o755)
        fixture = scenario.get('fixture')
        if fixture and request.get('fixtures'):
            fixture_path = Path(fixture)
            if fixture_path.is_absolute() or '..' in fixture_path.parts:
                raise ValueError('fixture leaves the declared fixture directory')
            extract_fixture(Path(request['fixtures']) / fixture_path, workspace)
        # Preserve fixture permission semantics while giving the observed user its own workspace.
        if uid is not None:
            for directory, dirs, files in os.walk(workspace, followlinks=False):
                for name in ['.', *dirs, *files]:
                    os.chown(Path(directory) / name, uid, uid, follow_symlinks=False)
        environment = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
        environment.update(request['environment'])
        environment.update(scenario.get('environment') or {})
        observed = []
        for step in scenario['steps']:
            cwd = workspace_directory(workspace, step.get('cwd', '.'), uid=uid)
            for token in step['argv'][1:]:
                token = str(token)
                if token and not token.startswith('-') and '/' in token:
                    parent = os.path.dirname(os.path.normpath(token.lstrip('./')))
                    if parent and not parent.startswith('..'):
                        workspace_directory(workspace, parent, uid=uid)
            args = step['argv']
            if args and args[0] == '{PROGRAM}':
                args = request['program'] + args[1:]
            else:
                import shlex
                program = shlex.join(request['program'])
                args = [str(value).replace('{PROGRAM}', program) for value in args]
            try:
                result = bounded_run(args, cwd=cwd, env=environment, input=step.get('stdin'),
                                     timeout=request['timeout'])
                code, out, err = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired:
                code, out, err = -1, '', '[timed out]'
            except OSError as error:
                code, out, err = 127, '', 'could not execute: %s' % error
            observed.append({'exit_code': code,
                             'stdout': out.replace(str(workspace), '<workspace>'),
                             'stderr': err.replace(str(workspace), '<workspace>'),
                             'tree': tree_lines(str(workspace), request['exclude'])})
        answers[scenario['probe_id']] = observed
        shutil.rmtree(workspace)
    return answers


def standalone_source():
    from .. import transport, isolated
    from . import snapshot
    source = Path(__file__).read_text()
    source = source[:source.index('\ndef standalone_source():')]
    source = (source.replace('from ..transport import bounded_run\n', '')
              .replace('from ..isolated import extract_fixture, workspace_directory\n', '')
              .replace('from .snapshot import tree_lines\n', ''))
    return (transport.standalone_source() + '\n' + isolated.standalone_source() + '\n'
            + snapshot.standalone_source() + '\n' + source
            + "\nif __name__ == '__main__':\n"
              "    request = json.loads(Path(sys.argv[1]).read_text())\n"
              "    payload = json.dumps(execute(request))\n"
              "    if len(payload.encode()) > 16 * 1024 * 1024:\n"
              "        raise ValueError('observation report exceeds 16 MiB')\n"
              "    Path(sys.argv[2]).write_text(payload)\n")
