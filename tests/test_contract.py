import pytest

from frf.core.contract import CheckoutContract, Contract, PackageContract, PackageOperation, Provenance


def test_contract_records_real_subject_and_auxiliary_boundary():
    contract = Contract("package", Provenance(
        subject_source="pypi:textdistance@4.6.3",
        contract_source="generator-validated",
        auxiliary_generated=True,
        evidence=("tests/test_utils.py",)), {"entry_points": ["find_ngrams"]})
    assert contract.to_json()["provenance"]["auxiliary_generated"] is True


def test_contract_refuses_a_generated_subject():
    with pytest.raises(ValueError, match="real sourced subject"):
        Contract("module", Provenance("model", "typed-signature")).validate()


def test_package_contract_is_broad_and_json_safe():
    ops = tuple(PackageOperation("op%d" % i, "pkg.mod", "op%d" % i) for i in range(4))
    contract = PackageContract("git:org/project@abc", "pkg", ops,
                               dependencies=("stdlib",),
                               provenance=Provenance("git:org/project@abc", "survey"))
    assert len(contract.to_json()["operations"]) == 4
    with pytest.raises(ValueError):
        PackageOperation("x", "pkg", "x", json_safe=False).validate()


def test_checkout_contract_keeps_real_dependency_context(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "hot.py").write_text("from . import helper\n")
    contract = CheckoutContract(
        str(tmp_path), ("src/hot.py",),
        verify=(("python", "-m", "pytest", "tests"),),
        provenance=Provenance("github:org/project@abc", "test-derived",
                              evidence=("tests/test_hot.py",)))
    assert contract.to_json()["target_paths"] == ["src/hot.py"]


def test_checkout_contract_is_scale_neutral(tmp_path):
    (tmp_path / "target").write_text("ok\n")
    for kind in ("module", "kernel", "package", "repo"):
        contract = CheckoutContract(str(tmp_path), ("target",), kind=kind,
            verify=(("true",),), provenance=Provenance("git:example/project@abc", "test-derived"))
        assert contract.to_json()["kind"] == kind


def test_checkout_performance_contract_requires_hidden_workspace_not_headroom(tmp_path):
    (tmp_path / "target").write_text("x")
    base = dict(root=str(tmp_path), target_paths=("target",), verify=(("true",),),
                benchmark=(("python3", "bench.py"),),
                provenance=Provenance("git:example/project@abc", "test"))
    with pytest.raises(ValueError, match="{workspace}"):
        CheckoutContract(**base).validate()
    valid = dict(base, benchmark=(("python3", "bench.py", "{workspace}"),))
    CheckoutContract(**valid).validate()
