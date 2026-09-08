import json
from types import SimpleNamespace

from frf.observe.call.package import coverage_manifest


def test_numeric_input_size_does_not_change_manifest_size():
    material = SimpleNamespace(symbol="dot")
    small = SimpleNamespace(inputs={"p": [[1., 2.]]}, timed=["p"])
    large = SimpleNamespace(inputs={"p": [[1.] * 65536]}, timed=["p"])
    assert coverage_manifest(small, material) == coverage_manifest(large, material)
    assert len(json.dumps(coverage_manifest(large, material))) < 120
    assert coverage_manifest(large, material)["operations"] == {"dot": 1}


def test_package_counts_declared_operations_only():
    material = SimpleNamespace(entry_points=["parse", "format"])
    corpus = SimpleNamespace(inputs={"a": ["parse", "/a"], "b": ["parse", "/b"],
                                     "c": ["format", {}], "bad": [[], 1]}, timed=["a"])
    assert coverage_manifest(corpus, material) == {
        "operations": {"parse": 2, "format": 1}, "probe_count": 4, "timed_count": 1}
