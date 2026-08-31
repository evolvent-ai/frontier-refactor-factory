"""A census of what has actually been produced, by language and scale.

WHY A CENSUS IS NOT A FILE COUNT. Roll mode runs each candidate as its own nested build, so one
subject that was frozen eight times with different probe counts leaves eight task directories. Counting
files therefore reported 105 tasks where there were 50 distinct subjects, and reported ten JavaScript
tasks where there was one JavaScript subject re-frozen ten times. A yield figure whose numerator counts
repeats is not a yield figure, so the unit here is the SUBJECT: one row per (scale, language, identity),
however many times that identity was rebuilt.

WHY ABSENT AND FAILED ARE NEVER MERGED. The point of this module is to be trustworthy about gaps. A
combination with no task at all, a task nobody attested, and a task whose battery failed are three
different facts, and collapsing any two of them produces the exact false confidence that made two
consecutive sessions re-derive the same "evidence still insufficient" conclusion by hand. So a cell
reports counts per status and never sums them into a single "done" number.

WHAT THIS MODULE WILL NOT DO. It does not promote anything. Reading a record is not the same as
deciding a combination is certified, and the decision belongs to whoever can weigh whether the
recorded backend and checks are enough. This produces the evidence table that decision is made from.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from . import attestation
from .capabilities import capability


# What is known about one emitted task.
#
# ATTESTED means a record exists AND every check in it held. Anything weaker is named for what it is:
# a record whose battery did not fully hold is `attested-failing`, and a task with no record at all is
# `unrecorded` -- which is a gap in our bookkeeping, not a statement about the task.
ATTESTED = "attested"
PARTIAL = "partial"
ATTESTED_FAILING = "attested-failing"
UNRECORDED = "unrecorded"

# Ordered best-first: a subject rebuilt several times keeps the strongest evidence any copy carries.
STATUSES = (ATTESTED, PARTIAL, ATTESTED_FAILING, UNRECORDED)

# THE CHECKS THAT MAKE A RECORD MEAN SOMETHING. "Every check held" is satisfiable by a record holding
# one cheap check, and a retroactive offline audit produces exactly that: a schema validation, no
# container, nothing about whether the task works. Reporting that as attested would manufacture the
# false confidence this module exists to prevent, so a record is only attested if it establishes that
# the reference reproduces its own expectations (E1), that a trivial submission does not (E2), and
# that the EMITTED package reproduces itself (E7). Those three cannot be obtained without running the
# subject, which is what makes them the dividing line.
DECISIVE_CHECKS = ("ceiling", "floor", "package-reproduces-itself")

# Decisive too, but only for a corpus produced WITH the gate. A task emitted before the gate existed
# carries no such verdict, and demanding one would retro-fail every task in the corpus rather than
# describe it -- so it is decisive when present and silent when absent, and `audit` reports which.
CONDITIONAL_CHECKS = ("reproduces-in-its-own-image",)


@dataclass(frozen=True)
class Subject:
    """One distinct emitted subject, with however much is known about how it was produced."""

    identity: str
    scale: str
    language: str
    status: str
    backend: str = ""
    checks_held: int = 0
    checks_total: int = 0
    copies: int = 1                 # how many task directories carry this same identity
    paths: tuple = ()

    @property
    def attested(self) -> bool:
        return self.status == ATTESTED


@dataclass
class Cell:
    """One (scale, language) combination."""

    scale: str
    language: str
    subjects: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.subjects)

    def counted(self, status: str) -> int:
        return sum(1 for s in self.subjects if s.status == status)

    @property
    def backends(self) -> list:
        """Which sandboxes produced the evidence here. Empty strings mean nobody recorded one."""
        return sorted({s.backend for s in self.subjects if s.backend})

    def to_json(self) -> dict:
        return {
            "scale": self.scale,
            "language": self.language,
            "subjects": self.total,
            # Task directories rather than subjects, so a reader can see how much of the output is
            # rebuilds of the same material.
            "task_directories": sum(s.copies for s in self.subjects),
            "by_status": {status: self.counted(status) for status in STATUSES},
            "backends": self.backends,
            "capability": capability(self.language, scale=self.scale).__dict__,
        }


def _metadata(task_toml: str) -> dict:
    try:
        with open(task_toml, "rb") as handle:
            return dict((tomllib.load(handle).get("metadata") or {}))
    except (OSError, ValueError):
        return {}


def _record_rank(record: dict) -> int:
    """How much a record establishes, for choosing among several for the same subject.

    Rank by the status `_status_from` would give it (a full battery outranks an offline backfill),
    then by proportion of checks held (a record that checked nothing ranks below one that checked
    something even if the something failed), then by how many checks ran.
    """
    status, _backend, held, total = _status_from({}, record)
    return (STATUSES.index(status), total > 0 and held / total, total)


def _status_from(meta: dict, record: dict) -> tuple[str, str, int, int]:
    """-> (status, backend, held, total), preferring the sidecar and falling back to the summary.

    The summary in `[metadata]` travels with a task that was copied away from its batch; the sidecar
    carries the detail. Neither is invented when both are missing.

    WHY A HELD BATTERY IS NOT AUTOMATICALLY ATTESTED. "held == total" is satisfied by a record that
    contains one cheap check, so an offline backfill would parade as fully audited. Only a record that
    establishes the decisive three -- and holds all of them -- is attested; anything else that ran is
    PARTIAL, which is a gap rather than a failure. A summary alone names no checks, so it cannot show
    decisive coverage and is reported as partial for that reason rather than promoted on trust.
    """
    if record:
        held = int(record.get("checks_held", 0))
        total = int(record.get("checks_total", 0))
        backend = str(record.get("backend", ""))
        outcomes = {str(c.get("check", "")): str(c.get("outcome", ""))
                    for c in record.get("checks", []) if isinstance(c, dict)}
    elif meta.get("evidence_schema") or meta.get("evidence_digest"):
        held = int(meta.get("evidence_checks_held", 0) or 0)
        total = int(meta.get("evidence_checks_total", 0) or 0)
        backend = str(meta.get("evidence_backend", "") or "")
        outcomes = {}
    else:
        return UNRECORDED, "", 0, 0

    # A battery with nothing in it must not read as success -- the rule `Battery.ok` also applies.
    if total <= 0 or held != total:
        return ATTESTED_FAILING, backend, held, total
    decisive_held = all(outcomes.get(name) in ("holds", "not-applicable")
                        for name in DECISIVE_CHECKS)
    # A conditional check that RAN and did not hold demotes the task; one that never ran does not.
    decisive_held = decisive_held and all(
        outcomes.get(name) in ("holds", "not-applicable")
        for name in CONDITIONAL_CHECKS if name in outcomes)
    return (ATTESTED if decisive_held else PARTIAL), backend, held, total


def walk(root: str) -> list[Subject]:
    """Every distinct subject under `root`, at any depth.

    Any depth because roll mode nests output under `.candidates/<hash>/`: the newest languages in this
    project's own output were sitting in hashed subdirectories while a flat listing of the batch
    directories showed nothing.
    """
    # ONE SUBJECT MAY CARRY SEVERAL RECORDS: a fresh build writes the full 8-check record, then a
    # backfill later adds a cheap 2-check one. A dict comprehension over all of them would keep
    # whichever `collect` reached last -- and os.walk order is not ours -- so the strongest record
    # per identity is selected here instead, and `walk` below reads that.
    records = {}
    for record in attestation.collect(root):
        name = str(record.get("task", ""))
        if not name:
            continue
        current = records.get(name)
        if current is None or _record_rank(record) < _record_rank(current):
            records[name] = record
    seen: dict[tuple, dict] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        if "task.toml" not in filenames:
            continue
        meta = _metadata(os.path.join(dirpath, "task.toml"))
        if not meta:
            continue
        identity = os.path.basename(dirpath)
        scale = str(meta.get("scale", "") or "unknown")
        language = str(meta.get("source_language", "") or "unknown")
        record = records.get(identity, {})
        status, backend, held, total = _status_from(meta, record)

        key = (scale, language, identity)
        found = seen.get(key)
        if found is None:
            seen[key] = {"status": status, "backend": backend, "held": held,
                         "total": total, "copies": 1, "paths": [dirpath]}
            continue
        # THE SAME SUBJECT REBUILT. Keep the best evidence any copy carries rather than whichever
        # copy `os.walk` reached last: a subject attested in one batch is an attested subject, and
        # letting directory order decide would make this report depend on the filesystem.
        found["copies"] += 1
        found["paths"].append(dirpath)
        if STATUSES.index(status) < STATUSES.index(found["status"]):
            found.update(status=status, backend=backend, held=held, total=total)

    return [Subject(identity=identity, scale=scale, language=language,
                    status=value["status"], backend=value["backend"],
                    checks_held=value["held"], checks_total=value["total"],
                    copies=value["copies"], paths=tuple(sorted(value["paths"])))
            for (scale, language, identity), value in sorted(seen.items())]


def matrix(subjects: list) -> list:
    """Group subjects into (scale, language) cells, ordered for a stable report."""
    cells: dict[tuple, Cell] = {}
    for subject in subjects:
        key = (subject.scale, subject.language)
        cells.setdefault(key, Cell(subject.scale, subject.language)).subjects.append(subject)
    return [cells[key] for key in sorted(cells)]


def report(root: str) -> dict:
    """The whole census, as a document that can be diffed between runs."""
    subjects = walk(root)
    cells = matrix(subjects)
    return {
        "root": root,
        "subjects": len(subjects),
        "task_directories": sum(s.copies for s in subjects),
        "by_status": {status: sum(1 for s in subjects if s.status == status)
                      for status in STATUSES},
        "cells": [cell.to_json() for cell in cells],
        # Languages the registry knows that produced nothing at all. A combination cannot be
        # certified on an empty cell, and an empty cell is invisible in a table built from output.
        "languages_without_output": sorted(
            set(capability(name).language for name in _registry_languages())
            - {s.language for s in subjects}),
    }


def _registry_languages() -> list:
    from .capabilities import snapshot
    return sorted(snapshot())


__all__ = ["ATTESTED", "ATTESTED_FAILING", "UNRECORDED", "STATUSES",
           "Subject", "Cell", "walk", "matrix", "report"]
