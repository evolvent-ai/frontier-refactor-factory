from frf.automation import BatchReport, _index, _scale, _merge_reports
from frf.core.diversity import DiversityPolicy, repository_key


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
