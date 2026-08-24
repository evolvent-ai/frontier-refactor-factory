from frf.automation import BatchReport, _index, _scale


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
