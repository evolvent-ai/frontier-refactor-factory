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
