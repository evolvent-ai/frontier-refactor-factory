"""Batch candidate diversity controls."""
from __future__ import annotations

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
