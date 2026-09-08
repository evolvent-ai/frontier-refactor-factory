"""Host admission checks for work whose execution happens in remote sandboxes."""
from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path


GiB = 1024 ** 3
_TRANSFER_SLOTS = threading.BoundedSemaphore(2)


class ResourcePressure(RuntimeError):
    """Do not start more work until host memory or storage headroom has recovered."""


def snapshot(paths=()) -> dict:
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, _, value = line.partition(':')
        fields = value.split()
        if fields:
            memory[key] = int(fields[0]) * 1024
    disks = {}
    for value in ('/', *paths):
        path = Path(value).resolve()
        while not path.exists() and path.parent != path:
            path = path.parent
        usage = shutil.disk_usage(path)
        disks[str(path)] = {'free': usage.free, 'total': usage.total}
    return {'available_memory': memory.get('MemAvailable', 0),
            'swap_used': memory.get('SwapTotal', 0) - memory.get('SwapFree', 0),
            'disks': disks}


def require_headroom(*paths, transfer_bytes=0, disk_bytes=0) -> dict:
    """Preserve host headroom before allocating a sandbox or buffering a transfer.

    Remote execution still stages data locally. Reserve extra memory and disk for transfers;
    disk snapshots are cheap and the check never deletes user data or stops existing processes.
    """
    minimum_memory = float(os.environ.get('FRF_HOST_MIN_MEMORY_GIB', '2')) * GiB
    minimum_disk = float(os.environ.get('FRF_HOST_MIN_DISK_GIB', '4')) * GiB
    if minimum_memory <= 0 or minimum_disk <= 0 or transfer_bytes < 0 or disk_bytes < 0:
        raise ValueError('host resource reserves must be positive')
    state = snapshot(paths)
    problems = []
    if state['available_memory'] < minimum_memory + 2 * transfer_bytes:
        problems.append('available memory %.2f GiB' % (state['available_memory'] / GiB))
    for path, disk in state['disks'].items():
        if disk['free'] < minimum_disk + max(transfer_bytes, disk_bytes):
            problems.append('free disk %.2f GiB at %s' % (disk['free'] / GiB, path))
    if problems:
        raise ResourcePressure('host resource admission refused: ' + '; '.join(problems))
    return state


@contextmanager
def transfer_slot(*paths):
    """Keep host archive CPU/I/O bounded independently of remote compute concurrency."""
    if not _TRANSFER_SLOTS.acquire(timeout=900):
        raise ResourcePressure('host transfer slots remained occupied for 900 seconds')
    try:
        require_headroom(*paths, transfer_bytes=16 * 1024 * 1024)
        yield
    finally:
        _TRANSFER_SLOTS.release()
