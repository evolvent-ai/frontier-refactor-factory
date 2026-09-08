"""Observe actual kernel exec events from a privileged process outside the subject root."""
import ctypes
import errno
import json
import os
import resource
import signal
import subprocess
import time
from pathlib import Path


class AuditedProcess:
    """Pipe-process interface used by the existing bounded transport."""
    def __init__(self, pid, argv, stdin, stdout, stderr, audit_path):
        self.pid, self.args, self.audit_path = pid, argv, audit_path
        self.stdin = os.fdopen(stdin, 'wb', buffering=0)
        self.stdout = os.fdopen(stdout, 'rb', buffering=0)
        self.stderr = os.fdopen(stderr, 'rb', buffering=0)
        self.returncode = None
        self.pidfd = os.pidfd_open(pid)

    def poll(self):
        if self.returncode is None:
            found, status = os.waitpid(self.pid, os.WNOHANG)
            if found:
                self.returncode = os.waitstatus_to_exitcode(status)
                os.close(self.pidfd)
                self.pidfd = None
        return self.returncode

    @property
    def subject_pid(self):
        with open(self.audit_path) as handle:
            event = json.loads(handle.readline(4096))
        if event.get('event') != 'spawn':
            raise RuntimeError('execution observer did not identify its subject')
        return event['pid']

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else float('inf')
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.001)
        return self.returncode

    def send_signal(self, number):
        if self.pidfd is not None:
            try:
                signal.pidfd_send_signal(self.pidfd, number)
            except ProcessLookupError:
                pass

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def terminate(self):
        self.send_signal(signal.SIGTERM)


def _supervise(root, argv, cwd, environment, streams, audit_path, allowed):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    live = set()
    main_status = None
    denied = False
    event_count = 0

    def ptrace(request, pid, address=0, data=0):
        result = libc.ptrace(request, pid, ctypes.c_void_p(address), ctypes.c_void_p(data))
        if result < 0:
            raise OSError(ctypes.get_errno(), 'execution observer ptrace operation failed')
        return result

    with open(audit_path, 'w', buffering=1) as log:
        def write(event):
            log.write(json.dumps(event, separators=(',', ':')) + '\n')
        try:
            libc.prctl(36, 1, 0, 0, 0)
            ready_read, ready_write = os.pipe()
            main = os.fork()
            if main == 0:
                try:
                    os.close(ready_write)
                    if os.read(ready_read, 1) != b'1':
                        os._exit(126)
                    for target, source in enumerate(streams):
                        os.dup2(source, target)
                    os.closerange(3, 1048576)
                    root.traceable = True
                    root._enter(cwd)
                    os.execvpe(argv[0], argv, environment)
                except BaseException:
                    os._exit(126)
            live.add(main)
            write({'event': 'spawn', 'pid': main})
            os.close(ready_read)
            try:
                ptrace(0x4206, main, data=(1 << 20) | 2 | 4 | 8 | 16)
                os.write(ready_write, b'1')
            finally:
                os.close(ready_write)
            while live:
                pid, status = os.waitpid(-1, 0x40000000)
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    live.discard(pid)
                    if pid == main:
                        main_status = os.waitstatus_to_exitcode(status)
                    continue
                event, number = status >> 16, os.WSTOPSIG(status)
                if event in (1, 2, 3):
                    child = ctypes.c_ulong()
                    ptrace(0x4201, pid, data=ctypes.addressof(child))
                    live.add(child.value)
                    if len(live) > 2048:
                        raise RuntimeError('execution observer process limit exceeded')
                elif event == 4:
                    previous_tid = ctypes.c_ulong()
                    ptrace(0x4201, pid, data=ctypes.addressof(previous_tid))
                    if previous_tid.value and previous_tid.value != pid:
                        live.discard(previous_tid.value)
                        live.add(pid)
                    event_count += 1
                    if event_count > 10000:
                        raise RuntimeError('execution observer event limit exceeded')
                    old_gid, old_uid = libc.setfsgid(root.uid), libc.setfsuid(root.uid)
                    try:
                        name = os.readlink('/proc/%d/exe' % pid)
                        info = os.stat('/proc/%d/exe' % pid)
                    finally:
                        libc.setfsuid(old_uid)
                        libc.setfsgid(old_gid)
                    accepted = allowed is None or (info.st_dev, info.st_ino) in allowed
                    write({'event': 'exec', 'pid': pid, 'path': name.replace(str(root.path), ''),
                           'device': info.st_dev, 'inode': info.st_ino, 'allowed': accepted})
                    if not accepted:
                        denied = True
                        for child in live:
                            try:
                                os.kill(child, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        continue
                if event == 128 and number in (signal.SIGSTOP, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
                    ptrace(0x4208, pid)
                else:
                    try:
                        ptrace(7, pid, data=0 if event else number)
                    except OSError as error:
                        if error.errno != errno.ESRCH:
                            raise
            write({'event': 'complete', 'main_status': main_status, 'denied': denied})
            return 126 if denied else (main_status if main_status is not None else 126)
        except BaseException as error:
            write({'event': 'failed', 'error_type': type(error).__name__})
            for pid in live:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return 126


def start_audited_process(root, argv, cwd, environment, audit_path, allowed):
    parent_pid = os.getpid()
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    all_fds = (stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write)
    try:
        pid = os.fork()
    except BaseException:
        for descriptor in all_fds:
            os.close(descriptor)
        raise
    if pid == 0:
        try:
            os.setsid()
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
                os._exit(126)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            keep = {0, 1, 2, stdin_read, stdout_write, stderr_write}
            for item in Path('/proc/self/fd').iterdir():
                descriptor = int(item.name)
                if descriptor not in keep:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            result = _supervise(root, argv, cwd, environment, (stdin_read, stdout_write, stderr_write),
                                audit_path, allowed)
            if result < 0:
                if -result not in (signal.SIGKILL, signal.SIGSTOP):
                    signal.signal(-result, signal.SIG_DFL)
                os.kill(os.getpid(), -result)
            os._exit(result)
        except BaseException:
            os._exit(126)
    for descriptor in (stdin_read, stdout_write, stderr_write):
        os.close(descriptor)
    return AuditedProcess(pid, argv, stdin_write, stdout_read, stderr_read, audit_path)


def standalone_source():
    return Path(__file__).read_text()
