"""The indexes, and the one mistake in them that would be invisible.

`core/sourcing.py` says candidates must come from an enumerable index. These are the clients that
make that affordable, and what is checked here is mostly the seam between them and `walk()` --
because that seam has a failure mode which produces no error, no log line and no exception, and
silently makes every yield figure afterwards wrong.

THE MISTAKE: `walk()` stops when a page comes back empty, since an empty page is how an index says
"exhausted". So a client that catches a timeout and returns `[]` does not report a network problem;
it reports that the registry has run out. The batch ends early, the coverage record says the supply
is gone, and nothing anywhere says otherwise. Half the tests below exist for that one line.

OFFLINE BY DEFAULT. The parsing is what breaks when a registry renames a field, and parsing can be
tested against a recorded payload with no network at all -- so it is, and those tests always run.
The live tests are opt-in through FRF_LIVE=1 and are skipped otherwise; they were run against all
seven registries while this was written, and what they found is recorded in the assertions.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                            # noqa: E402

from frf import source                                                   # noqa: E402
from frf.core import sourcing                                            # noqa: E402
from frf.source import filters, github, npm, pypi, rubygems              # noqa: E402
from frf.source.http import (Http, NotFound, SourceError, TransportError,  # noqa: E402
                             all_or_nothing)

LIVE = os.environ.get("FRF_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set FRF_LIVE=1 to reach the real registries")


class _Recorded:
    """An Http that answers from a script instead of a network.

    Not a mock of the parsing -- the parsing is the real thing under test. This replaces only the
    transport, which is the one part that cannot be exercised deterministically.
    """

    def __init__(self, payloads: list) -> None:
        self._payloads = list(payloads)
        self.calls = 0
        self.token = ""                     # GitHub now checks this attribute
        self.last_headers = {}              # GitHub now reads rate-limit headers from here

    def json(self, url: str, **_kwargs):
        self.calls += 1
        if not self._payloads:
            raise AssertionError("the client asked for %s, which was not scripted" % url)
        answer = self._payloads.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url: str, **_kwargs) -> bytes:
        return b""

    def lines(self, url: str, **_kwargs) -> list:
        return []


# One real npm row, recorded live. Kept verbatim rather than tidied: a payload edited to be
# convenient stops being evidence about what the registry sends.
_NPM_ROW = {
    "package": {
        "name": "yocto-queue", "version": "1.2.2",
        "description": "Tiny queue data structure",
        "keywords": ["queue", "data-structure", "algorithm"],
        "links": {"repository": "https://github.com/sindresorhus/yocto-queue"},
    },
}


def test_a_transport_failure_is_never_a_page_of_zero_rows():
    """THE test in this file. An empty page means exhausted, so a failure must raise.

    A client that swallowed this would end a batch early and write a coverage record saying the
    supply had run out -- with no exception, no log line, and every yield figure afterwards
    computed against a denominator that had quietly become wrong.
    """
    index = npm.Npm(_Recorded([TransportError("the registry did not answer")]))
    with pytest.raises(SourceError):
        index.page(0, size=5)


def test_walk_would_treat_an_empty_page_as_exhaustion():
    """The other half of the contract above, from `walk()`'s side.

    Stated as a test rather than as a comment because it is the reason the previous test matters:
    if `walk()` did not stop on an empty page, a client returning `[]` on failure would merely be
    unhelpful rather than silently corrupting the accounting.
    """
    class _Index:
        name = "empty-after-one"

        def __init__(self) -> None:
            self.pages = 0

        def page(self, number: int, *, size: int):
            self.pages += 1
            if number == 0:
                return [sourcing.Candidate("x:1", "module", "python", self.name)]
            return []

        def total(self):
            return None

    index = _Index()
    got = list(sourcing.walk(index, budget=50))
    assert len(got) == 1, "walk() kept asking after an empty page"
    assert index.pages == 2, "walk() should stop at the first empty page"


def test_one_missing_item_thins_a_page_but_a_page_of_them_raises():
    """A name can 404 between being listed and being fetched; fifty cannot, innocently.

    The asymmetry is the whole reason `all_or_nothing` exists rather than a comprehension in each
    client: losing one row is a thinner page, and losing every row is indistinguishable from
    exhaustion by the only consumer that matters.
    """
    def hydrate(row):
        if row == "gone":
            raise NotFound("no such project")
        return row

    assert all_or_nothing(["a", "gone", "b"], hydrate, index="test") == ["a", "b"]
    with pytest.raises(SourceError):
        all_or_nothing(["gone", "gone"], hydrate, index="test")


def test_an_index_that_will_not_say_how_big_it_is_reports_none():
    """None is honest and supported; a fabricated denominator is not.

    RubyGems publishes no count for a search. Returning the registry's overall gem count would be a
    number about something else, and returning pages-walked-so-far would describe this run.
    """
    assert rubygems.RubyGems(_Recorded([])).total() is None
    assert sourcing.Coverage("rubygems", walked=30, total=None).remaining is None


def test_a_candidate_carries_what_the_decision_was_made_on():
    """Facts travel in `detail` so that a rejection is auditable after the registry has moved on."""
    candidate = npm.to_candidate(_NPM_ROW["package"], {})
    assert candidate.language == "javascript"
    assert candidate.identity.startswith("npm:yocto-queue@")
    recorded = candidate.detail["facts"]
    assert recorded["name"] == "yocto-queue" and recorded["version"] == "1.2.2"
    assert filters.keep(candidate), filters.refusals(filters.Facts(
        name=recorded["name"], version=recorded["version"], summary=recorded["summary"]))


def test_facts_survive_the_round_trip_they_are_recorded_for():
    """A field a client sets and the round trip loses is worse than no field.

    `extra` holds what only one registry publishes -- Maven's packaging, npm's engines. It was
    dropped by `as_json` when this was written, so a check reading it would have seen an empty dict
    and passed everything, which is the failure mode that looks like success.
    """
    facts = filters.Facts(name="a", version="1.0", summary="x" * 20,
                          extra={"packaging": "jar"})
    assert filters.as_json(facts)["extra"] == {"packaging": "jar"}

    class _Candidate:
        detail = {"facts": filters.as_json(facts)}

    assert filters.keep(_Candidate())


def test_github_function_miner_refuses_non_python_call_adapters(tmp_path):
    from frf.source.function_miner import GitHubFunctions
    from frf.core.scale import Candidate

    miner = GitHubFunctions(object(), workspace=str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    miner.materialise = lambda *args, **kwargs: str(root)
    repository = Candidate("github:org/js@abc", "repo", "javascript", "github",
                           detail={"repository": "https://example.invalid/org/js",
                                   "commit": "abc", "identity": "org/js",
                                   "language": "JavaScript"})
    assert miner._widen(repository) == []
    assert miner.rejection_counts["no-drawable-functions"] == 1


def test_the_filters_say_which_check_refused():
    """A filter that only answers False teaches nothing about why a yield fell."""
    io_bound = filters.Facts(name="httpserver", version="1.0",
                             summary="an asynchronous HTTP server and client")
    assert "computational" in filters.refusals(io_bound)

    unversioned = filters.Facts(name="thing", version="", summary="a compression codec" * 2)
    assert "pinned release" in filters.refusals(unversioned)

    assert filters.accepts(filters.Facts(
        name="fastsort", version="2.1.0", summary="sorting and searching algorithms for arrays",
        dependencies=1, has_tests=True))


def test_an_unknown_metadata_field_is_accepted_rather_than_guessed():
    """Unknown is not the same as absent, and rejecting on it would exclude whole ecosystems.

    crates.io and Maven publish no test metadata at all. A filter that read None as "no tests"
    would refuse every Rust and Java candidate for a reason that is about the registry.
    """
    unknown = filters.Facts(name="matrixmath", version="0.4.0",
                            summary="dense linear algebra primitives",
                            has_tests=None, dependencies=None)
    assert filters.accepts(unknown), filters.refusals(unknown)


def test_a_repository_is_pinned_by_a_revision_and_a_package_is_not():
    """The same string means opposite things at the two scales, so the check distinguishes them.

    A repository has no releases: a clone at a revision is the strongest pin there is. A published
    package's untagged commit is material its authors never released, which is what Go's
    pseudo-version encodes -- so that one is still refused.
    """
    revision = "a" * 40
    assert filters.has_pinned_release(filters.Facts(name="repo", version=revision))
    assert not filters.has_pinned_release(
        filters.Facts(name="mod", version="v0.0.0-20191109021931-daa7c04131f5"))
    assert filters.has_pinned_release(filters.Facts(name="pkg", version="1.4.2"))


def test_github_segments_because_one_query_cannot_reach_past_a_thousand():
    """The wall is the reason this client is not fifty lines like the others.

    Search caps at 1000 results however many it says matched, so an unsegmented client walks a
    thousand repositories and reports the supply as exhausted with `total_count` in the millions.
    """
    index = github.GitHub(_Recorded([]), language="rust")
    assert index.reachable() == github.RESULT_CEILING * len(github.SEGMENTS)
    assert index.reachable() > github.RESULT_CEILING

    queries = {index._query_for(segment) for segment in github.SEGMENTS}
    assert len(queries) == len(github.SEGMENTS), "two segments would walk the same repositories"
    assert all("archived:false" in q for q in queries)


def test_github_total_counts_the_index_and_not_whichever_segment_ran_first():
    """A denominator that quietly means something narrower than it says is worse than none.

    This was real: caching `total_count` from the first request meant that after one page of the
    top star segment, `total()` reported the 29 repositories in THAT segment as the size of GitHub.
    """
    scripted = _Recorded([
        {"total_count": 29, "items": [{"full_name": "a/b", "default_branch": "main"}]},
        {"sha": "c" * 40},
        {"total_count": 1262955, "items": []},
    ])
    index = github.GitHub(scripted, language="rust")
    index.page(0, size=1)
    assert index.total() == 1262955, "total() reported a segment's count as the index's"


def test_a_github_candidate_identity_names_a_commit_and_not_a_branch():
    """`main` is not a version: it names wherever the project has got to.

    An identity that moves defeats `Memory` -- a repository whose contents have completely changed
    would be recognised as already tried -- and an expectation frozen against a branch describes
    whatever that branch was on the afternoon it ran.
    """
    row = {"full_name": "typst/typst", "default_branch": "main", "language": "Rust",
           "description": "A markup-based typesetting system", "clone_url": "https://x/y.git",
           "stargazers_count": 55490}
    pinned = github.to_candidate(row, commit="e" * 40)
    assert pinned.identity.endswith("@" + "e" * 40)
    assert filters.keep(pinned), "a pinned repository must survive the mechanical filters"

    unpinned = github.to_candidate(row)
    assert not filters.keep(unpinned), (
        "an unpinned repository must be refused: there is no version to write down")


def test_every_index_is_reachable_by_name():
    """The table is what makes "which indexes does this installation have" a printable question.

    Ten registries, all reachable by name. GitHub is the primary source for all four scales.
    """
    assert set(source.available()) == {"pypi", "npm", "crates.io", "pkg.go.dev", "maven",
                                       "rubygems", "github", "github-functions", "github-packages",
                                       "hackage", "opam", "hex.pm"}
    assert source.index_for("PyPI") is pypi.PyPI
    with pytest.raises(LookupError) as caught:
        source.index_for("cpan")
    assert "cpan" in str(caught.value) and "pypi" in str(caught.value)


def test_no_client_reads_a_credential_from_the_environment_directly():
    """One reader, so that a run does not depend on how it happened to be launched.

    GitHub is the only client with a credential and it must reach it through `credentials`, which
    also consults `.env`. A direct `os.environ` read works from a shell and fails from a scheduler.
    """
    import ast

    for name in os.listdir(os.path.join(ROOT, "frf", "source")):
        if not name.endswith(".py"):
            continue
        path = os.path.join(ROOT, "frf", "source", name)
        tree = ast.parse(open(path, encoding="utf-8").read())
        # Walked as a syntax tree rather than as text. The first version of this grepped the file
        # and flagged the module docstring of the one client that documents the rule -- prose
        # explaining that it does not read os.environ read, to a regex, exactly like doing so.
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if isinstance(base, ast.Name) and base.id == "os" and node.attr in ("environ",
                                                                                "getenv"):
                offenders.append("%s:%d os.%s" % (name, node.lineno, node.attr))
        assert not offenders, offenders


# ---------------------------------------------------------------------------------------------
# Live. Skipped unless FRF_LIVE=1. Every one of these was run against the real registry while this
# file was written; what they returned is recorded in the assertions rather than in a comment.
# ---------------------------------------------------------------------------------------------
@live_only
@pytest.mark.parametrize("name", ["pypi", "npm", "crates.io", "pkg.go.dev", "maven",
                                  "rubygems", "github"])
def test_live_an_index_yields_well_formed_candidates(name):
    index = source.index_for(name)()
    rows = list(index.page(0, size=5))
    assert rows, "%s returned nothing, which walk() would read as exhaustion" % name
    for candidate in rows:
        assert candidate.identity and candidate.scale and candidate.source
        assert isinstance(candidate.detail, dict) and candidate.detail.get("facts")


@live_only
@pytest.mark.parametrize("name", ["crates.io", "npm", "rubygems", "github"])
def test_live_paging_does_not_repeat_itself(name):
    """Two pages that overlap mean the index reordered under the walk, and resuming is meaningless."""
    index = source.index_for(name)()
    first = {c.identity for c in index.page(0, size=5)}
    second = {c.identity for c in index.page(1, size=5)}
    assert first and second
    assert not (first & second), "%s served the same candidate on two pages" % name


@live_only
def test_live_a_real_total_is_a_real_number():
    """crates.io publishes one, and it should look like a registry rather than like a page."""
    assert source.index_for("crates.io")().total() > 100000


@live_only
def test_live_a_404_raises_rather_than_returning_empty():
    """The transport's contract, against the real thing."""
    with pytest.raises(NotFound):
        Http().json("https://pypi.org/pypi/this-package-does-not-exist-frf/json")


def test_a_wide_chain_reports_its_total_as_unknown_rather_than_paying_for_it():
    """Each link's `total()` is a network call, and an unfiltered walk builds sixty-three links.

    GitHub allows thirty searches a minute, so summing them does not merely cost time -- it
    exhausts the budget the paging then needs. A real batch sat at zero attempts for twenty minutes
    with kernel and package starving exactly there, while module (whose chain is narrower) ran fine.

    Unknown is already the honest answer in this module: `Coverage` prints it as unknown rather
    than inventing a denominator.
    """
    from frf.source.chain import Chain, QuotaChain

    class Link:
        name = "link"
        asked = 0

        def total(self):
            Link.asked += 1
            return 10

        def page(self, number, *, size=20):
            return []

    narrow = Chain([Link() for _ in range(3)])
    assert narrow.total() == 30
    assert Link.asked == 3, "a narrow chain still reports a real denominator"

    Link.asked = 0
    wide = Chain([Link() for _ in range(63)])
    assert wide.total() is None, "a chain this wide must not be summed"
    assert Link.asked == 0, "and must not have asked a single link"

    Link.asked = 0
    assert QuotaChain([Link() for _ in range(63)]).total() is None
    assert Link.asked == 0
