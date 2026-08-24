"""Sourcing: the memory, the accounting, and the rule that makes scalability answerable."""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf.core import sourcing                                          # noqa: E402
from frf.core.scale import Candidate                                   # noqa: E402


class _FakeIndex:
    """A countable, stably-ordered index -- the only kind sourcing accepts."""

    name = "test-index"

    def __init__(self, count: int = 120) -> None:
        self.count = count
        self.pages_served = 0

    def page(self, number: int, *, size: int):
        self.pages_served += 1
        start = number * size
        return [Candidate("repo://%d" % i, "module", "python", self.name)
                for i in range(start, min(start + size, self.count))]

    def total(self) -> int:
        return self.count


def test_it_yields_the_budget_and_stops_paging():
    """A generator, so an index is not walked for material the batch stopped needing."""
    index = _FakeIndex(1000)
    found = list(sourcing.walk(index, budget=7, page_size=10))

    assert len(found) == 7
    assert index.pages_served == 1, "seven from a page of ten needs exactly one page"


def test_what_was_tried_before_is_not_tried_again():
    """Without memory a run rediscovers what the last one refused, and the yield falls for a reason
    that has nothing to do with the material."""
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "seen.json")

        first = list(sourcing.walk(_FakeIndex(), budget=5, memory=sourcing.Memory.load(path)))
        second = list(sourcing.walk(_FakeIndex(), budget=5, memory=sourcing.Memory.load(path)))

        assert {c.identity for c in first} & {c.identity for c in second} == set()
        assert len(sourcing.Memory.load(path)) == 10, "the record survives the process"


def test_the_mechanical_filter_runs_before_anything_is_cloned():
    """`keep` is cheap and deterministic. Judgement about quality belongs to the gates, which
    answer with evidence rather than with a prediction."""
    even = list(sourcing.walk(_FakeIndex(), budget=4,
                              keep=lambda c: int(c.identity.rsplit("/", 1)[-1]) % 2 == 0))
    assert [c.identity for c in even] == ["repo://0", "repo://2", "repo://4", "repo://6"]


def test_an_exhausted_index_stops_rather_than_looping():
    found = list(sourcing.walk(_FakeIndex(3), budget=100, page_size=2))
    assert len(found) == 3, "asking for more than exists yields what exists"


def test_an_index_that_will_not_say_its_size_is_still_usable():
    """None means unknown, and unknown is honest. Refusing such an index would trade a real supply
    for a tidy number."""
    class _Uncountable(_FakeIndex):
        def total(self):
            raise RuntimeError("this index does not publish a count")

    found = list(sourcing.walk(_Uncountable(20), budget=3))
    assert len(found) == 3

    coverage = sourcing.Coverage("x", walked=10, total=None)
    assert coverage.remaining is None
    assert sourcing.Coverage("x", walked=10, total=100).remaining == 90


def test_walk_exposes_machine_readable_coverage_on_the_index():
    index = _FakeIndex(3)
    assert len(list(sourcing.walk(index, budget=2, page_size=2))) == 2
    coverage = index.last_coverage
    assert coverage.to_json() == {
        "index": "test-index", "walked": 2, "fresh": 2, "repeats": 0,
        "total": 3, "remaining": 1,
    }


def test_walk_can_stop_at_a_wall_clock_deadline():
    index = _FakeIndex(100)
    found = list(sourcing.walk(index, budget=100, max_seconds=0))
    assert found == []
    assert index.last_coverage.walked == 0
