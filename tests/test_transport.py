"""Subprocess output and partial frames cannot bypass deadlines or memory bounds."""
import subprocess
import sys
import time

import pytest

from frf.observe.transport import OutputLimitExceeded, PipeTransport, bounded_run


def test_one_shot_drains_stderr_while_writing_input_and_preserves_it():
    command = [sys.executable, '-c',
               "import sys; sys.stderr.write('e'*100000); sys.stderr.flush(); "
               "print(len(sys.stdin.buffer.read()))"]
    done = bounded_run(command, input='a' * 2000000, timeout=5)
    assert done.returncode == 0 and done.stdout == '2000000\n'
    assert done.stderr == 'e' * 100000


def test_output_flood_is_bounded_and_process_is_reaped():
    with pytest.raises(OutputLimitExceeded):
        bounded_run([sys.executable, '-c', "import os,time; os.write(1,b'x'*100000); time.sleep(10)"],
                    timeout=2, output_limit=4096)


def test_partial_json_line_cannot_extend_deadline():
    process = subprocess.Popen([sys.executable, '-c',
                                "import sys,time; sys.stdin.readline(); print('{',end='',flush=True); time.sleep(10)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            PipeTransport(process).exchange('{}\n', timeout=0.2, line=True)
        assert time.monotonic() - started < 2
    finally:
        process.kill()
        process.communicate()


def test_json_service_stderr_does_not_deadlock_repeated_requests():
    process = subprocess.Popen([sys.executable, '-c',
                                "import sys,json\nfor line in sys.stdin:\n"
                                " sys.stderr.write('e'*100000);sys.stderr.flush()\n"
                                " print(json.dumps(json.loads(line)),flush=True)\n"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        transport = PipeTransport(process, output_limit=200000)
        for index in range(3):
            assert transport.exchange(str(index) + '\n', timeout=3, line=True) == str(index)
        assert len(transport.stderr_tail) == 8192
    finally:
        process.kill()
        process.communicate()
