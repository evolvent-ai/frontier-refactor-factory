"""Carry replay evidence without changing the public (passed, total) tuple interface."""
import hashlib
import json
import os
from pathlib import Path

from ..core.evidence import Outcome, Verdict
from .isolated import ISOLATION_CHECKS


class ReplayResult(tuple):
    def __new__(cls, passed, total, report):
        value = super().__new__(cls, (passed, total))
        value.report = report
        return value


def _task_digest(path):
    """Bind replay reuse to file contents, modes, directory entries and link targets."""
    from .in_image import SKIP_DIRS
    root = Path(path)
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(dirs + files):
            item = Path(directory) / name
            info = item.lstat()
            header = [item.relative_to(root).as_posix(), info.st_mode]
            if item.is_symlink():
                header.append(os.readlink(item))
            elif item.is_file():
                content = hashlib.sha256()
                with item.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        content.update(chunk)
                header.append(content.hexdigest())
            digest.update(json.dumps(header, separators=(',', ':')).encode() + b'\n')
    return digest.hexdigest()


class ImageReplay:
    """Share one remote delivered-image replay between E7, E6 and E9."""

    def __init__(self, backend, log=lambda _message: None, *, offline=False, image_export=None):
        self.backend = getattr(backend, 'control_backend', backend)
        self.log = log
        self.offline = offline
        self.image_export = image_export
        self._record = None
        self._path = None
        self._digest = None

    def replay(self, path):
        from .in_image import drive
        self._record = None
        self._path = os.path.abspath(path)
        self._digest = _task_digest(path)
        record = drive(path, backend=self.backend, log=self.log, offline=self.offline,
                       image_export=self.image_export)
        if not record.get('ok'):
            raise RuntimeError('delivered-image replay failed (%s): %s' %
                               (record.get('stage'), record.get('detail')))
        if _task_digest(path) != self._digest:
            raise RuntimeError('task changed during delivered-image replay')
        report = record.get('report') or {}
        result = ReplayResult(record['passed'], record['total'], dict(report))
        verdict = execution_evidence(path, result)
        if verdict.outcome is not Outcome.HOLDS:
            raise RuntimeError(verdict.detail)
        result.report['delivered_image'] = {
            'image_id': record['image_id'], 'task_sha256': self._digest,
            'passed': record['passed'], 'total': record['total'],
        }
        self._record = record
        return result

    def image_check(self, path):
        if (self._record is None or os.path.abspath(path) != self._path
                or _task_digest(path) != self._digest):
            return {'ok': False, 'stage': 'changed-artifact',
                    'detail': 'no delivered-image replay for the current task contents'}
        return self._record


def execution_evidence(path, result):
    name = 'cannot-delegate-to-the-reference'
    report = getattr(result, 'report', {})
    isolation = report.get('isolation') or {}
    checks = isolation.get('checks') or {}
    try:
        digest = hashlib.sha256((Path(path) / 'tests/verify.py').read_bytes()).hexdigest()
    except OSError:
        digest = None
    if not digest or report.get('verifier_sha256') != digest:
        return Verdict(name, Outcome.INCONCLUSIVE, 'replay is not bound to the current verifier bytes')
    if (report.get('correct') is not True or report.get('timing_valid') is not True
            or report.get('correctness_total', 0) <= 0
            or report.get('correctness_passed') != report.get('correctness_total')):
        return Verdict(name, Outcome.INCONCLUSIVE, 'correct and timed replay is required for isolation acceptance')
    if isolation.get('enforced') is not True or any(checks.get(key) is not True for key in ISOLATION_CHECKS):
        return Verdict(name, Outcome.INCONCLUSIVE, 'the emitted evaluator did not demonstrate all isolation checks')
    return Verdict(name, Outcome.HOLDS,
                   'current verifier replay passed in separately confined roots; file/network/signal access, '
                   'idle-side pause/resume and probe cleanup checked in those roots')
