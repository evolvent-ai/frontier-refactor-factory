"""Static contradictions that runtime replay alone cannot establish or rule out."""
import json
from pathlib import Path


def contract_findings(path: Path, metadata: dict) -> list[dict]:
    """Reject known artifact contradictions; this is not runtime certification."""
    findings = []

    def read(relative):
        try:
            value = json.loads((path / relative).read_text())
            if not isinstance(value, dict):
                raise ValueError('object required')
            return value
        except (OSError, ValueError):
            findings.append({'kind': 'invalid-contract-file', 'path': relative})
            return {}

    is_repo = metadata.get('scale') == 'repo'
    cross = metadata.get('cross_language') is True
    if cross:
        target = metadata.get('target_language')
        declaration = read('tests/environment.json' if is_repo else 'tests/expectations.json')
        workspace = read('environment/task-interface.json')
        if not target or target == metadata.get('source_language'):
            findings.append({'kind': 'invalid-cross-language-metadata'})
        if not target or declaration.get('target_language') != target:
            findings.append({'kind': 'cross-grader-target-mismatch'})
        if not target or workspace.get('target_language') != target:
            findings.append({'kind': 'cross-workspace-target-mismatch'})
        if not (path / 'environment/implementation').is_dir():
            findings.append({'kind': 'cross-implementation-missing'})
        if not (path / 'tests/reference-runtime.json').is_file():
            findings.append({'kind': 'cross-reference-runtime-missing'})

    # A successful replay can still time a correctness input. Check the actual partitions,
    # rather than trusting Instruction's claim that performance workloads are held out.
    if is_repo and (path / 'tests/timed.json').is_file():
        graded = read('tests/expectations.json')
        frozen_timing = read('tests/timed_expectations.json')
        try:
            timed = json.loads((path / 'tests/timed.json').read_text())
            if not isinstance(timed, list) or not all(isinstance(p, str) for p in timed):
                raise ValueError('probe IDs required')
        except (OSError, ValueError):
            findings.append({'kind': 'invalid-contract-file', 'path': 'tests/timed.json'})
            timed = []
    elif not is_repo and (path / 'tests/expectations.json').is_file():
        frozen = read('tests/expectations.json')
        try:
            graded = {e['probe_id'] for e in frozen.get('graded', []) if not e.get('dropped')}
            timed = frozen.get('timed', [])
            if not isinstance(timed, list) or not all(isinstance(p, str) for p in timed):
                raise ValueError('probe IDs required')
            frozen_timing = frozen.get('timed_expectations', {})
            if not isinstance(frozen_timing, dict):
                raise ValueError('timed expectations must be an object')
        except (KeyError, TypeError, AttributeError, ValueError):
            findings.append({'kind': 'invalid-contract-file', 'path': 'tests/expectations.json'})
            return findings
    else:
        return findings
    overlap = sorted(set(timed).intersection(graded))
    if overlap:
        findings.append({'kind': 'timing-overlaps-correctness', 'probe_ids': overlap})
    missing = sorted(set(timed).difference(frozen_timing))
    if missing:
        findings.append({'kind': 'timing-baseline-missing', 'probe_ids': missing})
    return findings
