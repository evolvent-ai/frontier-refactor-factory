from frf.automation import BatchReport, _index, _scale, _merge_reports
from frf.config import JobConfig, RunConfig
from frf.core.diversity import DiversityPolicy, repository_key
import hashlib


def test_diversity_policy_limits_one_repository_without_losing_identity():
    assert repository_key("github:org/repo@abc#pkg.fn") == "github:org/repo"
    policy = DiversityPolicy(max_per_repository=2)
    assert [policy.accept("github:org/repo@abc#fn%d" % i) for i in range(3)] == [True, True, False]


def test_default_indexes_and_scale_wiring():
    assert _index("pypi", subset="algorithm").name == "pypi"
    assert _index("github", subset="algorithm").name == "github"
    assert _scale("module", object()).name == "module"
    assert _scale("kernel", object()).name == "kernel"
    assert _scale("package", object()).name == "package"
    assert _scale("repo", object()).name == "repo"


def test_scale_receives_the_requested_backend():
    backend = object()
    assert _scale("module", object(), backend=backend)._backend is backend
    assert _scale("package", object(), backend=backend)._backend is backend


def test_batch_report_is_json_ready():
    report = BatchReport({"scale": "module", "attempted": 1}, 1.23456, "github")
    assert report.to_json() == {"scale": "module", "attempted": 1,
                                "seconds": 1.235, "index": "github"}


def test_roll_report_keeps_source_rejection_reasons():
    reports = [
        BatchReport({"scale": "package", "attempted": 1, "emitted": 0,
                     "trustworthy": True, "source_rejections": {"checkout-failed": 2}},
                    1, "github-packages"),
        BatchReport({"scale": "package", "attempted": 1, "emitted": 1,
                     "trustworthy": True, "source_rejections": {"checkout-failed": 3,
                                                                  "surface-too-small": 1}},
                    1, "github-packages"),
    ]
    merged = _merge_reports(reports, "github-packages", 2)
    assert merged.summary["source_rejections"] == {
        "checkout-failed": 5, "surface-too-small": 1}


def test_empty_source_is_reported_separately_from_zero_yield():
    from frf.automation import BatchReport
    report = BatchReport({"attempted": 0, "emitted": 0,
                          "source_eligibility": "empty",
                          "source_note": "index returned no eligible candidates"}, 0.2, "github")
    assert report.summary["source_eligibility"] == "empty"


def test_roll_attempt_limit_round_trips_through_config():
    cfg = RunConfig.from_dict({"jobs": [{"scale": "repo", "form": "inplace",
                                          "budget": 3, "max_attempts": 21}]})
    assert cfg.jobs[0].max_attempts == 21
    assert cfg.to_json()["jobs"][0]["max_attempts"] == 21


def test_same_candidate_isolated_per_scale_and_form():
    identity = "github:org/repo@abc#module.fn"
    module = hashlib.sha256(("module|inplace||" + identity).encode()).hexdigest()[:12]
    kernel = hashlib.sha256(("kernel|inplace||" + identity).encode()).hexdigest()[:12]
    cross = hashlib.sha256(("module|cross|rust|" + identity).encode()).hexdigest()[:12]
    assert len({module, kernel, cross}) == 3


def test_roll_attempt_limit_must_cover_the_emitted_target():
    try:
        JobConfig("repo", "inplace", budget=4, max_attempts=3)
    except ValueError as exc:
        assert "smaller" in str(exc)
    else:
        raise AssertionError("an impossible roll budget must be rejected before sourcing")


def test_config_rejects_single_freeze_pass_before_a_batch_starts():
    try:
        RunConfig(jobs=[], freeze_runs=1)
    except ValueError as exc:
        assert "at least 2" in str(exc)
    else:
        raise AssertionError("one freeze pass cannot establish reproducibility")


def test_roll_continues_past_refusals_and_stops_at_the_emitted_target(monkeypatch):
    import frf.automation as automation
    from frf.core.scale import Candidate

    candidates = [Candidate("github:org/repo%d@abc" % i, "repo", "go", "test")
                  for i in range(8)]

    class SourceScale:
        def find(self, budget):
            return candidates[:budget]

    outer_run = automation.run
    monkeypatch.setattr(automation, "_index", lambda *args, **kwargs: object())
    monkeypatch.setattr(automation, "_scale", lambda *args, **kwargs: SourceScale())
    attempted = []

    def build_one(*args, candidates, **kwargs):
        identity = candidates[0].identity
        attempted.append(identity)
        emitted = int(identity.endswith(("repo2@abc", "repo4@abc")))
        return BatchReport({"scale": "repo", "attempted": 1, "emitted": emitted,
                            "yield_rate": float(emitted), "trustworthy": True,
                            "refused_material": 1 - emitted, "refused_factory": 0,
                            "by_reason": {}}, 0.01, "github")

    monkeypatch.setattr(automation, "run", build_one)
    report = outer_run("repo", budget=2, target_emitted=True, max_attempts=6,
                       candidate_workers=3)

    assert report.summary["emitted"] == 2
    assert report.summary["target_met"] is True
    assert len(attempted) == 5


def test_a_repo_walk_does_not_lead_with_topics_that_barely_exist():
    """Order is supply, not only label precision, and getting it backwards costs a whole batch.

    `Chain` is deliberately depth-first: the first topic is spent before the second is touched, so a
    budget is consumed in list order. An earlier version led with the most precisely-labelled
    topics -- jq, yq, csvkit, pandoc -- which is right about the labels and wrong about the outcome,
    because outside Python those topics hold a handful of repositories each. A real Rust batch
    refused 13 candidates and every one was a jq clone; with `attempt_limit = 30` it could never
    reach `parser` (304 Rust repositories) or `compiler` (425), which sat at positions 16 and 17.
    """
    from frf.automation import TRANSFORMER_TOPICS, _SCARCE_TOPICS

    leading = TRANSFORMER_TOPICS[:6]
    assert not _SCARCE_TOPICS & set(leading), (
        "a topic measured to hold almost nothing must not spend the budget first: %s" % (leading,))
    # The plentiful transformers -- each reads a text and writes a determined text -- lead instead.
    assert set(("parser", "compiler")) <= set(leading), leading
    # And the topics measured to return TUIs stay last, where they cost little.
    assert TRANSFORMER_TOPICS[-2:] == ("cli", "command-line"), TRANSFORMER_TOPICS[-2:]
    # Every scarce topic is still walked; this is an ordering claim, not a filter.
    assert _SCARCE_TOPICS <= set(TRANSFORMER_TOPICS)


def test_a_requested_language_is_never_widened_back_to_python():
    """The constraint the whole open-world design rests on: no silent fallback to Python.

    `GitHub` appends its own `language:` from its keyword argument, so a query string that names one
    too sends two -- and GitHub reads repeated qualifiers as OR, not AND. `--scale kernel
    --source rust` was measured returning 608 repositories: 84 Rust plus all 524 Python ones, so six
    candidates in seven were the language the caller had explicitly not asked for.
    """
    def query_of(scale, subset):
        index = _index("github-functions", subset=subset, scale=scale)
        # The inner index is now a Chain over topics; its first link carries the language qualifier
        # that the test checks.
        return index._index._links[0]._query_for(None)

    rust = query_of("kernel", "rust")
    assert "language:rust" in rust
    assert "language:python" not in rust, rust
    assert rust.count("language:") == 1, "two qualifiers mean OR, which widens rather than narrows"

    # Python remains the default when nothing was requested -- that is where kernel evidence exists.
    assert "language:python" in query_of("kernel", "")
    # Module asks for no language of its own, so an unconstrained walk stays open-world.
    assert "language:" not in query_of("module", "")


def test_function_scale_sourcing_chains_topics_rather_than_one_algorithms_search():
    """Diversity is not a quality improvement; it is what a yield measures.

    `topic:algorithms` alone was the whole supply for kernel/module/package, so every batch drew
    from the same puzzle-library family and a "make it faster" task on the twentieth LeetCode
    clone measured nothing the first nineteen didn't. Several topics chained -- algorithms,
    data-structures, string, math, matrix, graph, text-processing, datetime, geometry --
    spread a batch over distinct families of code.
    """
    from frf.automation import FUNCTION_TOPICS, _chain_of_topics
    class _Index:
        def __init__(self, *, language="", query="", scale=""):
            self.language, self.query, self.scale = language, query, scale
        def name(self):
            return self.query
    assert len(FUNCTION_TOPICS) >= 8, "a single-topic supply is the concentration this exists to avoid"
    assert "algorithms" in FUNCTION_TOPICS    # it still leads; it is no longer alone
    assert len(set(FUNCTION_TOPICS)) == len(FUNCTION_TOPICS), "duplicate topics waste a search"

    chain = _chain_of_topics(_Index, ("a", "b"), "rust", scale="module")
    # The chain name names every topic, so the coverage log says what was actually searched.
    assert "a|b" in chain.name


def test_every_scale_walks_more_than_one_topic_by_default():
    """The four scales are not four consumers of one search.

    Repo and function scales walk their own topic lists; the open-world package chain enumerates
    several language chains. None may fall back to a single search, because that is how the
    corpus stops being a corpus.
    """
    from frf.automation import _chain_of_topics, FUNCTION_TOPICS, TRANSFORMER_TOPICS
    class _Index:
        def __init__(self, *, language="", query="", scale=""):
            self.language, self.query, self.scale = language, query, scale
        def name(self):
            return self.query
    repo = _chain_of_topics(_Index, TRANSFORMER_TOPICS, "go", scale="repo")
    funcs = _chain_of_topics(_Index, FUNCTION_TOPICS, "typescript", scale="module")
    assert "|" in repo.name and "|" in funcs.name
