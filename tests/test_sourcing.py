"""Sourcing: the memory, the accounting, and the rule that makes scalability answerable."""
from __future__ import annotations

import os
import sys
import tempfile
import json

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


def test_checkpoint_resume_retries_factory_failures(tmp_path):
    from frf.core.checkpoint import CheckpointRecord, CheckpointWriter
    path = str(tmp_path / "run.jsonl")
    writer = CheckpointWriter(path)
    base = dict(scale="module", task_form="", stage="", reason="", timestamp="now", path="")
    writer.write(CheckpointRecord(identity="emitted", status="emitted", fault="", **base))
    writer.write(CheckpointRecord(identity="material", status="refused", fault="material", **base))
    writer.write(CheckpointRecord(identity="factory", status="refused", fault="factory", **base))
    assert writer.load_completed() == {"emitted", "material"}


def test_batch_ledger_is_append_only_and_jsonl(tmp_path):
    from frf.core.ledger import BatchLedger, LedgerRecord
    path = str(tmp_path / "ledger.jsonl")
    ledger = BatchLedger(path)
    ledger.append(LedgerRecord("x", "module", "refused", fault="material"))
    ledger.append(LedgerRecord("y", "module", "emitted", path="tasks/y"))
    rows = [json.loads(line) for line in open(path)]
    assert [row["identity"] for row in rows] == ["x", "y"]


def test_a_refusal_records_what_actually_failed_not_only_its_category(tmp_path):
    """`reason` is a category; `detail` is what to fix, and it used to be dropped on the floor.

    A batch that refused thirteen candidates left thirteen rows saying only which stage said no.
    Diagnosing it meant guessing from repository names -- which is how a sourcing bug stayed
    invisible for two sessions: every row read as ordinary unusable material.
    """
    from frf.core.ledger import BatchLedger, LedgerRecord
    path = str(tmp_path / "ledger.jsonl")
    BatchLedger(path).append(LedgerRecord(
        "nom", "repo", "refused", stage="specify", reason="could-not-specify",
        detail="no runnable entry point: this crate is a library"))
    row = json.loads(open(path).read())
    assert "library" in row["detail"], "the particular failure has to survive the write"


def test_one_pathological_detail_cannot_dominate_the_ledger(tmp_path):
    """Bounded so a stack trace cannot bury every other row, and so a line stays atomic."""
    from frf.core.ledger import BatchLedger, LedgerRecord, DETAIL_LIMIT
    path = str(tmp_path / "ledger.jsonl")
    BatchLedger(path).append(LedgerRecord("x", "repo", "refused", detail="!" * 50_000))
    assert len(json.loads(open(path).read())["detail"]) == DETAIL_LIMIT


def test_concurrent_candidates_do_not_interleave_their_rows(tmp_path):
    """A batch runs candidates in parallel, and each row must land whole or not at all.

    The previous writer copied the entire file and renamed it over the original for every row, which
    is quadratic and also loses this property: two writers racing that way overwrite each other.
    """
    from concurrent.futures import ThreadPoolExecutor
    from frf.core.ledger import BatchLedger, LedgerRecord
    path = str(tmp_path / "ledger.jsonl")
    ledger = BatchLedger(path)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: ledger.append(
            LedgerRecord("cand-%03d" % i, "repo", "refused", detail="d" * 200)), range(200)))
    rows = [json.loads(line) for line in open(path)]
    assert len(rows) == 200, "every row survived"
    assert len({r["identity"] for r in rows}) == 200, "and none was overwritten by another"


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


def test_successive_finds_advance_instead_of_reserving_the_same_page():
    """A roll asks for candidates, tries one, and asks again. It must not get the same one back.

    `walk` restarts its paging at zero on every call, and an index maps page 0 to the same first
    page for ever -- correctly, since "asking twice is a question with a stable answer" is the rule
    this module exists to enforce. With a fresh `Memory` per call the second ask returns the first
    ask's candidates, the roll's own seen-set filters all of them out, and the loop concludes the
    index is spent.

    MEASURED, NOT HYPOTHESISED: three cells configured `max_attempts: 15` reported `attempted: 1`
    and were read as thin material.
    """
    from frf.core import sourcing
    from frf.core.scale import Candidate

    class Pages:
        name = "pages"

        def __init__(self):
            self.fetches = 0

        def total(self):
            return 12

        def page(self, number, *, size=50):
            self.fetches += 1
            start = number * 4
            if start >= 12:
                return []
            return [Candidate(identity="c%d" % n, scale="repo", language="go", source="pages")
                    for n in range(start, min(start + 4, 12))]

    class Scale:
        """Stands in for a real scale: one index, reused across find() calls."""

        def __init__(self):
            self._index = Pages()

        def find(self, budget):
            return sourcing.walk(self._index, budget, page_size=4,
                                 memory=sourcing.batch_memory(self))

    scale = Scale()
    first = [c.identity for c in scale.find(4)]
    second = [c.identity for c in scale.find(4)]
    third = [c.identity for c in scale.find(4)]

    assert first == ["c0", "c1", "c2", "c3"], first
    assert not set(first) & set(second), "the second ask re-served the first ask's candidates"
    assert not (set(first) | set(second)) & set(third), third
    assert len(set(first) | set(second) | set(third)) == 12


def test_a_scale_without_the_shared_memory_repeats_itself():
    """The failing behaviour, pinned, so the fix above cannot be quietly undone.

    Same index, same asks, no shared memory -- and the second call hands back exactly what the
    first did. This is what made a fifteen-attempt roll stop after one.
    """
    from frf.core import sourcing
    from frf.core.scale import Candidate

    class Pages:
        name = "pages"

        def total(self):
            return 8

        def page(self, number, *, size=50):
            start = number * 4
            if start >= 8:
                return []
            return [Candidate(identity="c%d" % n, scale="repo", language="go", source="pages")
                    for n in range(start, min(start + 4, 8))]

    index = Pages()
    first = [c.identity for c in sourcing.walk(index, 4, page_size=4)]
    second = [c.identity for c in sourcing.walk(index, 4, page_size=4)]
    assert first == second, "this test documents the old behaviour; if it changed, update the fix"


def test_the_real_scales_advance_across_successive_finds():
    """The shipped scales, not a stand-in: each must page forward on the second ask.

    The stub above proves the mechanism; this proves the three classes actually use it. Without it
    a scale could quietly drop `memory=` and every test here would still pass while a roll went
    back to making one attempt out of fifteen.
    """
    from frf.core.scale import Candidate
    from frf.scales.module import Module
    from frf.scales.package import Package
    from frf.scales.repo import Repo

    def index_for(scale_name):
        class Pages:
            name = "pages"

            def total(self):
                return 8

            def page(self, number, *, size=50):
                start = number * 4
                if start >= 8:
                    return []
                return [Candidate(identity="c%d" % n, scale=scale_name, language="go",
                                  source="pages",
                                  # repo's `keep` reads these; absent, it refuses every row and the
                                  # test would pass for the wrong reason.
                                  detail={"size_kb": 1, "commit": "a" * 40,
                                          "facts": {"version": "a" * 40, "name": "n",
                                                    "summary": "a summary long enough to pass"}})
                        for n in range(start, min(start + 4, 8))]
        return Pages()

    for cls, scale_name in ((Module, "module"), (Package, "package"), (Repo, "repo")):
        scale = cls(index=index_for(scale_name))
        first = [c.identity for c in scale.find(4)]
        second = [c.identity for c in scale.find(4)]
        assert first, "%s found nothing at all" % scale_name
        assert not set(first) & set(second), (
            "%s re-served its first page: %s then %s" % (scale_name, first, second))


def test_a_restarted_roll_can_continue_the_supply_instead_of_re_mining_its_head(tmp_path, monkeypatch):
    """Restarting re-walked from page one and re-produced what it had already produced.

    Measured across four restarts of one batch: 55 emissions collapsed to 20 distinct subjects, so
    roughly two thirds of the sandbox time was spent on material already emitted. `Memory` was built
    to carry a seen-set between runs and nothing had ever given it a path.
    """
    from frf.core import sourcing
    from frf.core.scale import Candidate

    store = tmp_path / "seen.json"
    monkeypatch.setenv("FRF_SOURCING_MEMORY", str(store))

    class Owner:
        pass

    first = sourcing.batch_memory(Owner())
    first.remember(Candidate(identity="github:a/b@c1", scale="repo", language="go", source="x"))
    first.save()
    assert store.exists()

    # A LATER PROCESS, which is what a restart is: a fresh owner reading the same file.
    resumed = sourcing.batch_memory(Owner())
    assert Candidate(identity="github:a/b@c1", scale="repo", language="go",
                     source="x") in resumed, "a restart must not rediscover what it already walked"
    assert Candidate(identity="github:a/b@c2", scale="repo", language="go",
                     source="x") not in resumed

    # Unset, it stays in-process -- the right default for a one-shot run.
    monkeypatch.delenv("FRF_SOURCING_MEMORY")
    assert len(sourcing.batch_memory(Owner())) == 0


def test_a_widening_index_is_told_what_is_already_spent():
    """Dedup after `page()` is free for a registry and ruinous for a widening index.

    `github-functions` downloads and tree-sitter-mines every repository a page names BEFORE
    yielding anything, and `walk` filters afterwards -- so a seeded seen-set pays the whole mining
    cost for material it then discards. A restarted roll spent nine minutes re-mining repositories
    it had already produced from and made one attempt.

    The hook is best-effort, like `last_coverage`: an index that ignores it still gets correct
    dedup from the loop below, just slowly.
    """
    from frf.core import sourcing
    from frf.core.scale import Candidate
    from frf.source.functions import _already_drawn_from

    class Widening:
        name = "widening"
        already_seen = None

        def total(self):
            return 2

        def page(self, number, *, size=50):
            if number:
                return []
            return [Candidate(identity="github:a/b@c1", scale="module", language="go",
                              source="widening")]

    index = Widening()
    memory = sourcing.Memory(seen={"github:a/b@c1#src/x.go.F"})
    list(sourcing.walk(index, 1, memory=memory))
    assert index.already_seen is not None, "walk must offer the seen-set to the index"
    assert "github:a/b@c1#src/x.go.F" in index.already_seen

    # And the widening index's own predicate treats a repository as spent once anything from it was.
    assert _already_drawn_from("github:a/b@c1", index.already_seen)
    assert not _already_drawn_from("github:other/repo@c9", index.already_seen)
