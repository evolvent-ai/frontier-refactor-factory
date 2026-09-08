"""Filesystem-confined subject processes managed by a trusted Linux verifier.

Requires a root verifier inside a resource-limited container. This primitive does not certify
the caller's timing protocol, target-language build, or output/descendant lifecycle management.
"""
from __future__ import annotations

import ctypes
import json
import errno
import os
import resource
import signal
import shutil
import stat
import subprocess
import tempfile
import tarfile
import threading
import time
from pathlib import Path
from .transport import bounded_run
from .exec_audit import start_audited_process


RUNTIME_PATHS = ('usr', 'opt', 'bin', 'sbin', 'lib', 'lib64', 'etc/alternatives',
                 'etc/ld.so.cache', 'etc/ld.so.conf', 'etc/ld.so.conf.d',
                 'etc/nsswitch.conf', 'etc/localtime', 'etc/ssl/certs')
_RESERVED_UIDS = set()
_UID_LOCK = threading.Lock()
ISOLATION_CHECKS = ('distinct_users', 'unconfined_control_readable', 'outside_read_denied', 'network_denied',
                    'reference_signal_denied', 'idle_side_stopped', 'idle_side_resumed',
                    'probe_processes_removed')


def extract_fixture(path, destination, *, max_members=10000, max_bytes=64 * 1024 * 1024):
    """Extract bounded data archives without version-specific extractall safety defaults."""
    root = Path(destination)
    total = 0
    links = []
    with tarfile.open(path) as archive:
        for count, member in enumerate(archive, 1):
            name = Path(member.name)
            if count > max_members or name.is_absolute() or '..' in name.parts:
                raise ValueError('unsafe or oversized fixture archive')
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise ValueError('special files are not allowed in fixtures')
            total += member.size
            if total > max_bytes or member.size < 0:
                raise ValueError('fixture data exceeds its byte budget')
            if member.isdir():
                workspace_directory(root, str(name))
                continue
            workspace_directory(root, str(name.parent))
            target = root / name
            if member.issym() or member.islnk():
                link = Path(member.linkname)
                resolved = Path(os.path.normpath(str(name.parent / link if member.issym() else link)))
                if link.is_absolute() or '..' in resolved.parts:
                    raise ValueError('fixture link leaves its workspace')
                links.append((target, member.linkname, root / resolved, member.issym()))
                continue
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as output, archive.extractfile(member) as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
                os.fchmod(output.fileno(), member.mode & 0o777)
        # Links are created only after all writes, so no archive member is written through a link.
        for target, _, resolved, symbolic in links:
            if not symbolic:
                workspace_directory(root, str(resolved.relative_to(root).parent))
                if not resolved.is_file() or resolved.is_symlink():
                    raise ValueError('fixture hardlink has no regular target')
                os.link(resolved, target, follow_symlinks=False)
        for target, link, _, symbolic in links:
            if symbolic:
                target.symlink_to(link)


def workspace_directory(workspace, relative, uid=None):
    """Privileged preparation must not follow a subject-created directory symlink."""
    relative = Path(os.path.normpath(relative))
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('working directory leaves the scenario workspace')
    current = Path(workspace)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('workspace preparation cannot traverse a symlink')
        if not current.exists():
            current.mkdir()
            if uid is not None:
                os.chown(current, uid, uid)
        elif not current.is_dir():
            raise ValueError('working directory is not a directory')
    return str(current)


def _processes_for(uid, *, include_zombies=False):
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            lines = (path / 'status').read_text().splitlines()
        except (OSError, UnicodeError):
            continue
        values = {key: value.strip() for key, _, value in (line.partition(':') for line in lines)}
        if (str(uid) in values.get('Uid', '').split()[:3]
                and (include_zombies or not values.get('State', '').startswith('Z'))):
            yield int(path.name)


def _copy_runtime(source, target):
    """Only root-owned, non-writable runtime files may share inodes with the verifier."""
    info = os.stat(source, follow_symlinks=False)
    if info.st_uid != 0:
        raise ValueError('untrusted runtime file owner: ' + str(source))
    if info.st_mode & 0o022:
        shutil.copy2(source, target, follow_symlinks=False)
        os.chmod(target, stat.S_IMODE(info.st_mode) & ~0o022)
        return str(target)
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        shutil.copy2(source, target, follow_symlinks=False)
    return str(target)


def _own_tree(root, uid):
    for directory, dirs, files in os.walk(root, followlinks=False):
        os.chown(directory, uid, uid)
        for name in dirs + files:
            os.chown(os.path.join(directory, name), uid, uid, follow_symlinks=False)


class Root:
    """A private root and workspace. Source symlinks remain symlinks inside the new root."""

    def __init__(self, source, *, uid, runtime_paths=RUNTIME_PATHS, runtime_base='/', runtime_environment=None):
        if os.geteuid() != 0:
            raise PermissionError('subject confinement requires a root verifier')
        if uid <= 0:
            raise ValueError('the subject uid must be unprivileged')
        if not (Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()):
            raise RuntimeError('subject roots must be managed inside a Linux container')
        for relative in runtime_paths:
            if Path(relative).is_absolute() or '..' in Path(relative).parts:
                raise ValueError('runtime paths must stay within the new root')
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise RuntimeError('pidfd support is required for subject cleanup')
        self.uid = uid
        self.runtime_environment = dict(runtime_environment or {})
        base = Path(runtime_base)
        if not base.is_absolute() or base.is_symlink() or not base.is_dir():
            raise ValueError('runtime base must be an absolute directory')
        with _UID_LOCK:
            if uid in _RESERVED_UIDS or next(_processes_for(uid), None) is not None:
                raise ValueError('subject uid is already in use')
            _RESERVED_UIDS.add(uid)
        try:
            self.room = Path(tempfile.mkdtemp(prefix='frf-isolated-'))
            self.path = self.room / 'root'
            self.path.mkdir(mode=0o755)
        except BaseException:
            with _UID_LOCK:
                _RESERVED_UIDS.discard(uid)
            if hasattr(self, 'room'):
                shutil.rmtree(self.room, ignore_errors=True)
            raise
        self.closed = False
        self.processes = []
        self.execution_audit = None
        self.execution_logs = []
        try:
            self._libc = ctypes.CDLL(None, use_errno=True)
            self._libc.syscall.restype = ctypes.c_long
            if self._libc.prctl(36, 1, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), 'child subreaper setup failed')
            for relative in runtime_paths:
                source_path = base / relative
                target = self.path / relative
                if not source_path.exists() and not source_path.is_symlink():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if source_path.is_symlink():
                    target.symlink_to(os.readlink(source_path))
                elif source_path.is_dir():
                    shutil.copytree(source_path, target, symlinks=True, copy_function=_copy_runtime)
                    for directory, _dirs, _files in os.walk(target, followlinks=False):
                        info = os.stat(directory, follow_symlinks=False)
                        os.chmod(directory, stat.S_IMODE(info.st_mode) & ~0o022)
                else:
                    if relative == 'etc/ld.so.cache':
                        shutil.copy2(source_path, target)
                    else:
                        _copy_runtime(source_path, target)
            # Multi-stage images can add shared libraries after their base cache was built.
            # Keep this cache private and rebuild it without modifying shared library symlinks.
            linker = shutil.which('ldconfig')
            if linker is None:
                raise RuntimeError('ldconfig is required to prepare the confined runtime')
            configured = subprocess.run([linker, '-r', str(self.path), '-X'],
                                        capture_output=True, text=True, timeout=60)
            if configured.returncode != 0:
                raise RuntimeError('confined runtime linker configuration failed: ' + configured.stderr[-500:])
            shutil.copytree(source, self.path / 'app', symlinks=True)
            _own_tree(self.path / 'app', uid)
            for name in ('tmp', 'workspace', 'home/subject', 'dev/shm'):
                directory = self.path / name
                directory.mkdir(parents=True, exist_ok=True)
                os.chown(directory, uid, uid)
            etc = self.path / 'etc'
            etc.mkdir(exist_ok=True)
            (etc / 'passwd').write_text('subject:x:%d:%d::/home/subject:/bin/sh\n' % (uid, uid))
            (etc / 'group').write_text('subject:x:%d:\n' % uid)
            for name, minor in (('null', 3), ('zero', 5), ('random', 8), ('urandom', 9)):
                device = self.path / 'dev' / name
                os.mknod(device, stat.S_IFCHR | 0o666, os.makedev(1, minor))
                os.chmod(device, 0o666)
            self._seccomp = ctypes.CDLL('libseccomp.so.2', use_errno=True)
            self._seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
            self._seccomp.seccomp_init.restype = ctypes.c_void_p
            self._seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
            self._seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                     ctypes.c_int, ctypes.c_uint]
            self._seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
            self._seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        except BaseException:
            self.close()
            raise

    def prepare(self, fixture_dir=None):
        """Prepare one invocation outside the measured interval."""
        if self.closed:
            raise RuntimeError('subject root is closed')
        workspace = self.path / 'workspace'
        shutil.rmtree(workspace)
        if fixture_dir:
            shutil.copytree(fixture_dir, workspace, symlinks=True)
        else:
            workspace.mkdir()
        _own_tree(workspace, self.uid)

    def audit_execution(self, paths=None):
        """Observe actual executable inodes and optionally restrict their allowlist."""
        if self.closed or self.execution_audit is not None:
            raise RuntimeError('execution observer cannot replace an active policy')
        allowed = None
        if paths is not None:
            allowed = set()
            for name in paths:
                relative = Path(name)
                if not relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('approved executable must be inside its subject root')
                path = self.path / str(relative).lstrip('/')
                if not path.resolve().is_relative_to(self.path.resolve()):
                    raise ValueError('approved executable escapes its subject root')
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError('approved executable must be immutable and root owned')
                allowed.add((info.st_dev, info.st_ino))
            if not allowed:
                raise ValueError('execution observer requires an approved entry point')
        self.execution_audit = {'allowed': allowed, 'paths': None if paths is None else list(paths)}

    def execution_report(self):
        events = []
        complete = bool(self.execution_logs)
        denied = False
        for path in self.execution_logs:
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                complete = False
                continue
            try:
                records = [json.loads(line) for line in path.read_text().splitlines()]
            except (OSError, ValueError):
                complete = False
                continue
            complete = complete and bool(records) and records[-1].get('event') == 'complete'
            denied = denied or any(record.get('allowed') is False or record.get('denied') is True
                                   for record in records)
            events.extend(record for record in records if record.get('event') == 'exec')
        return {'enabled': self.execution_audit is not None, 'complete': complete, 'denied': denied,
                'enforced': self.execution_audit is not None and self.execution_audit['allowed'] is not None,
                'events': events}

    def _enter(self, cwd):
        os.chroot(self.path)
        os.chdir(cwd)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NPROC, (512, 512))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
        os.setgroups([])
        os.setgid(self.uid)
        os.setuid(self.uid)
        if self._libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'no_new_privs failed')
        if getattr(self, 'traceable', False) and self._libc.prctl(4, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'execution audit visibility failed')
        context = self._seccomp.seccomp_init(0x7fff0000)
        if not context:
            raise OSError('seccomp initialization failed')
        try:
            # Anonymous socket pairs only connect a process to its own descendants;
            # Rust uses them when spawning compiler/worker processes. Creating sockets
            # that can connect to an external or reference endpoint remains forbidden.
            for name in (b'socket', b'ptrace', b'process_vm_readv', b'process_vm_writev',
                         b'unshare', b'setns', b'mount', b'umount2', b'pivot_root', b'chroot',
                         b'bpf', b'userfaultfd', b'io_uring_setup', b'keyctl', b'add_key', b'request_key',
                         b'perf_event_open', b'open_by_handle_at', b'pidfd_getfd', b'shmget', b'shmat',
                         b'msgget', b'msgsnd', b'msgrcv', b'semget', b'semop', b'semtimedop'):
                syscall = self._seccomp.seccomp_syscall_resolve_name(name)
                if syscall >= 0 and self._seccomp.seccomp_rule_add(context, 0x00050001, syscall, 0) != 0:
                    raise OSError('seccomp rule installation failed')
            if self._seccomp.seccomp_load(context) != 0:
                raise OSError('seccomp loading failed')
        finally:
            self._seccomp.seccomp_release(context)

    def spawn(self, argv, *, cwd='/workspace', environment=None):
        if self.closed:
            raise RuntimeError('subject root is closed')
        if not cwd.startswith('/'):
            raise ValueError('subject cwd must be absolute inside its root')
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/home/subject',
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC'}
        env.update(environment or {})
        env.update(self.runtime_environment)
        env['HOME'] = '/home/subject'
        if self.execution_audit is not None:
            path = self.room / ('execution-%d.jsonl' % len(self.execution_logs))
            self.execution_logs.append(path)
            process = start_audited_process(self, argv, cwd, env, path, self.execution_audit['allowed'])
            self.processes.append(process)
            return process
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env, close_fds=True,
                                   start_new_session=True, preexec_fn=lambda: self._enter(cwd))
        self.processes.append(process)
        return process

    def _signal(self, pid, number):
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            status = Path('/proc/%d/status' % pid).read_text()
            uid_line = next(line for line in status.splitlines() if line.startswith('Uid:'))
            if str(self.uid) in uid_line.split()[1:4]:
                signal.pidfd_send_signal(descriptor, number)
        except (ProcessLookupError, FileNotFoundError):
            pass
        finally:
            os.close(descriptor)

    def run(self, argv, *, cwd='/workspace', environment=None, input=None, timeout=300):
        self.resume()
        return bounded_run(argv, input=input, timeout=timeout,
                           spawn=lambda: self.spawn(argv, cwd=cwd, environment=environment),
                           stop=lambda finished: self.pause() if finished else self.stop())

    def pause(self):
        if self.closed:
            raise RuntimeError('subject root is closed')
        deadline = time.monotonic() + 5
        while True:
            for pid in _processes_for(self.uid):
                self._signal(pid, signal.SIGSTOP)
            stopped = True
            for pid in _processes_for(self.uid):
                try:
                    lines = Path('/proc/%d/status' % pid).read_text().splitlines()
                except FileNotFoundError:
                    continue
                state = next(line for line in lines if line.startswith('State:')).split()[1]
                stopped = stopped and state in ('T', 't')
            if stopped:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('untimed subject did not stop')
            time.sleep(0.005)

    def resume(self):
        if self.closed:
            raise RuntimeError('subject root is closed')
        for pid in _processes_for(self.uid):
            self._signal(pid, signal.SIGCONT)

    def stop(self):
        if not self.closed:
            deadline = time.monotonic() + 5
            while True:
                pids = list(_processes_for(self.uid))
                if not pids:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('subject processes survived cleanup; terminate its container')
                for pid in pids:
                    self._signal(pid, signal.SIGKILL)
                time.sleep(0.01)
            for process in self.processes:
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            for pid in _processes_for(self.uid, include_zombies=True):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            self.processes.clear()

    def close(self):
        if not self.closed:
            self.stop()
            shutil.rmtree(self.room, ignore_errors=True)
            with _UID_LOCK:
                _RESERVED_UIDS.discard(self.uid)
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def standalone_source():
    from .exec_audit import standalone_source as execution_source
    return (execution_source() + '\n' + Path(__file__).read_text(encoding='utf-8')
            .replace('from __future__ import annotations\n', '')
            .replace('from .transport import bounded_run\n', '')
            .replace('from .exec_audit import start_audited_process\n', '')
            .replace('    from .exec_audit import standalone_source as execution_source\n', ''))


def verify_pair(reference, candidate):
    """Check the exact roots used for grading; all probes finish before task execution starts."""
    if reference.uid == candidate.uid or reference.path == candidate.path:
        raise RuntimeError('reference and candidate do not have distinct roots and users')
    descriptor, marker_name = tempfile.mkstemp(prefix='frf-isolation-control-')
    marker = Path(marker_name)
    secret = 'private-' + os.urandom(24).hex()
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(secret)
        os.fchmod(handle.fileno(), 0o644)
    heartbeat = reference.path / 'workspace/heartbeat'
    code = ("import pathlib,time,os; p=pathlib.Path('/workspace/heartbeat'); i=0\n"
            "while True:\n i+=1; p.write_text(str(i)); time.sleep(0.02)\n")
    reference.prepare()
    candidate.prepare()
    try:
        control = bounded_run(['python3', '-c', 'import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())',
                               str(marker)], timeout=10,
                              spawn=lambda: subprocess.Popen(
                                  ['python3', '-c', 'import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())',
                                   str(marker)], user=candidate.uid, group=candidate.uid, extra_groups=[],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  start_new_session=True))
        if control.returncode != 0 or control.stdout.strip() != secret:
            raise RuntimeError('unconfined control could not read the isolation marker')
        holding = reference.spawn(['python3', '-c', code])
        deadline = time.monotonic() + 5
        while not heartbeat.exists() or not heartbeat.read_text():
            if holding.poll() is not None:
                raise RuntimeError('reference isolation control exited %s: %s' %
                                   (holding.returncode, holding.stderr.read(4096)))
            if time.monotonic() > deadline:
                raise RuntimeError('reference isolation control timed out')
            time.sleep(0.01)
        reference.pause()
        count = heartbeat.read_text()
        time.sleep(0.05)
        stopped = heartbeat.read_text() == count
        attack = '''import json,os,pathlib,socket,sys
result={'uid':os.getuid()}
try:
 pathlib.Path(sys.argv[1]).read_bytes(); result['outside_read_denied']=False
except FileNotFoundError:
 result['outside_read_denied']=True
try:
 socket.socket(socket.AF_INET,socket.SOCK_STREAM); result['network_denied']=False
except PermissionError:
 result['network_denied']=True
try:
 os.kill(int(sys.argv[2]),0); result['reference_signal_denied']=False
except PermissionError:
 result['reference_signal_denied']=True
print(json.dumps(result))
'''
        reference_pid = getattr(holding, 'subject_pid', holding.pid)
        done = candidate.run(['python3', '-c', attack, str(marker), str(reference_pid)], timeout=10)
        if done.returncode != 0:
            raise RuntimeError('candidate isolation control failed: ' + done.stderr[-500:])
        checks = json.loads(done.stdout)
        status = Path('/proc/%d/status' % reference_pid).read_text().splitlines()
        reference_uid = next(line for line in status if line.startswith('Uid:')).split()[1]
        reference.resume()
        deadline = time.monotonic() + 5
        while not heartbeat.read_text() or heartbeat.read_text() == count:
            if holding.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('reference control did not resume')
            time.sleep(0.01)
        checks.update(distinct_users=checks.pop('uid') == candidate.uid and int(reference_uid) == reference.uid,
                      unconfined_control_readable=True,
                      idle_side_stopped=stopped, idle_side_resumed=True)
    finally:
        candidate.stop()
        reference.stop()
        marker.unlink(missing_ok=True)
    checks['probe_processes_removed'] = not (
        list(_processes_for(reference.uid, include_zombies=True)) or
        list(_processes_for(candidate.uid, include_zombies=True)))
    if any(checks.get(name) is not True for name in ISOLATION_CHECKS):
        raise RuntimeError('subject isolation check failed: ' + json.dumps(checks))
    reference.prepare()
    candidate.prepare()
    return {'checks': checks, 'enforced': True}
