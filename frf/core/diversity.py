"""Batch candidate diversity controls."""
from __future__ import annotations

import threading
from collections import Counter


def repository_key(identity: str) -> str:
    value = str(identity).split("#", 1)[0]
    return value.rsplit("@", 1)[0]


class DiversityPolicy:
    """Bound repeated source concentration without changing the enumerable denominator."""

    def __init__(self, *, max_per_repository: int = 4):
        self.max_per_repository = max(1, int(max_per_repository))
        self._repositories = Counter()

    def accept(self, identity: str) -> bool:
        key = repository_key(identity)
        if self._repositories[key] >= self.max_per_repository:
            return False
        self._repositories[key] += 1
        return True

    def summary(self) -> dict:
        return {"repositories": len(self._repositories),
                "max_repeated": max(self._repositories.values(), default=0),
                "counts": dict(self._repositories)}


# THE CAP MUST OUTLIVE ONE JOB, or splitting a scale by language quietly removes it.
#
# Two rules pulled against each other in a real batch. `max_per_repository` counted per job, so
# splitting a scale reset it and one repository could be taken to the cap again in every job. But an
# unfiltered walk ranks by stars and the top of that ranking is python-heavy -- a package job left
# open produced fourteen tasks and every one was python -- so a multi-language corpus NEEDS the
# split. The advice that came out of it ("split only when you must") asked a person to hold two
# facts in their head, which is how the next batch loses one of them.
#
# So the counter is shared instead. Jobs asking for the same scale see one policy, and the cap means
# what it says however the batch is arranged: four language jobs of five tasks now bound
# concentration exactly as one job of twenty would.
#
# KEYED BY SCALE, not globally: a repository that serves a module task and a repo task is two
# different subjects observed through different seams, and charging them to one budget would refuse
# the second for no reason a reader could act on.
_SHARED: dict = {}
_SHARED_LOCK = threading.Lock()


def shared_policy(scale: str, *, max_per_repository: int) -> "DiversityPolicy":
    """The policy every job for `scale` shares. Created on first use.

    The first caller's cap wins; a later job asking for a different one is a configuration mistake
    and is reported rather than silently honoured, because the two answers cannot both hold.
    """
    with _SHARED_LOCK:
        existing = _SHARED.get(scale)
        if existing is None:
            existing = DiversityPolicy(max_per_repository=max_per_repository)
            _SHARED[scale] = existing
        elif existing.max_per_repository != max_per_repository:
            print("[diversity] %s jobs disagree on max_per_repository (%d vs %d); keeping %d"
                  % (scale, existing.max_per_repository, max_per_repository,
                     existing.max_per_repository), flush=True)
        return existing


def reset_shared() -> None:
    """Forget every shared policy. For tests, and for a process that runs more than one batch."""
    with _SHARED_LOCK:
        _SHARED.clear()
