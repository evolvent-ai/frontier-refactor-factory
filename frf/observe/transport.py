"""Bounded, deadline-aware subprocess pipe transport for one-shot and JSON-lines subjects."""
import os
import selectors
import subprocess
import time


class OutputLimitExceeded(RuntimeError):
    pass


class PipeTransport:
    def __init__(self, process, *, output_limit=64 * 1024 * 1024):
        self.process = process
        self.output_limit = output_limit
        self.pending = bytearray()
        self.stderr_tail = bytearray()
        self.stderr_output = bytearray()
        self.readers = {process.stdout.fileno(): 'stdout', process.stderr.fileno(): 'stderr'}
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)

    def exchange(self, payload, *, timeout, line=False):
        data = payload.encode('utf-8') if isinstance(payload, str) else (payload or b'')
        if line and self.pending.strip():
            raise RuntimeError('subject emitted an unsolicited response')
        deadline = time.monotonic() + timeout
        offset = 0
        output = bytearray(self.pending)
        received = len(output)
        self.stderr_output.clear()
        self.pending.clear()
        with selectors.DefaultSelector() as poll:
            for descriptor, channel in self.readers.items():
                poll.register(descriptor, selectors.EVENT_READ, channel)
            if data and not self.process.stdin.closed:
                poll.register(self.process.stdin.fileno(), selectors.EVENT_WRITE, 'stdin')
            elif not line and not self.process.stdin.closed:
                self.process.stdin.close()
            while poll.get_map():
                if line and offset == len(data) and b'\n' in output:
                    answer, _, rest = output.partition(b'\n')
                    self.pending.extend(rest)
                    return answer.decode('utf-8')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self.process.args, timeout)
                for key, _ in poll.select(remaining):
                    if key.data == 'stdin':
                        try:
                            offset += os.write(key.fd, data[offset:offset + 4096])
                        except BrokenPipeError:
                            if line:
                                raise RuntimeError('subject closed input before receiving the request')
                            offset = len(data)
                        if offset == len(data):
                            poll.unregister(key.fd)
                            if not line:
                                self.process.stdin.close()
                        continue
                    block = os.read(key.fd, 65536)
                    if not block:
                        poll.unregister(key.fd)
                        self.readers.pop(key.fd, None)
                        continue
                    received += len(block)
                    if received > self.output_limit:
                        raise OutputLimitExceeded('subject output exceeds %d bytes' % self.output_limit)
                    if key.data == 'stdout':
                        output.extend(block)
                    else:
                        if not line:
                            self.stderr_output.extend(block)
                        self.stderr_tail.extend(block)
                        if len(self.stderr_tail) > 8192:
                            del self.stderr_tail[:-8192]
            if line:
                if offset == len(data) and b'\n' in output:
                    answer, _, rest = output.partition(b'\n')
                    self.pending.extend(rest)
                    return answer.decode('utf-8')
                raise RuntimeError('subject exited without a complete response')
            # Keep the exited leader unreaped until its process group has been cleaned up.
            while os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.process.args, timeout)
                time.sleep(0.001)
            return output.decode('utf-8', 'surrogateescape')


def bounded_run(argv, *, cwd=None, env=None, input=None, timeout=300, output_limit=64 * 1024 * 1024,
                spawn=None, stop=None):
    process = (spawn() if spawn is not None else subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True))
    transport = PipeTransport(process, output_limit=output_limit)
    finished = False
    try:
        out = transport.exchange(input, timeout=timeout)
        err = transport.stderr_output.decode('utf-8', 'surrogateescape')
        finished = True
    finally:
        if stop is not None:
            stop(finished)
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return subprocess.CompletedProcess(argv, process.returncode,
                                       out.replace('\r\n', '\n').replace('\r', '\n'),
                                       err.replace('\r\n', '\n').replace('\r', '\n'))


def standalone_source():
    from pathlib import Path
    return Path(__file__).read_text(encoding='utf-8')
