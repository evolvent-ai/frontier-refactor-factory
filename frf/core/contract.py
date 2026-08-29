"""Validated contracts for sourced module, package and repository subjects."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provenance:
    subject_source: str
    contract_source: str
    auxiliary_generated: bool = False
    evidence: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"subject_source": self.subject_source, "contract_source": self.contract_source,
                "auxiliary_generated": self.auxiliary_generated, "evidence": list(self.evidence)}


@dataclass(frozen=True)
class Contract:
    kind: str
    provenance: Provenance
    data: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not self.provenance.subject_source or self.provenance.subject_source in ("generated", "model"):
            raise ValueError("a contract must name a real sourced subject")
        if not self.provenance.contract_source:
            raise ValueError("contract source is required")
        if not self.kind:
            raise ValueError("contract kind is required")

    def to_json(self) -> dict:
        self.validate()
        return {"kind": self.kind, "provenance": self.provenance.to_json(), "data": self.data}


@dataclass(frozen=True)
class PackageOperation:
    name: str
    module: str
    symbol: str
    signature: str = ""
    json_safe: bool = True
    # HOW A RUBY INSTANCE IS MADE, when the operation is an instance method. Ruby gems expose almost
    # everything as instance methods on a class, and those cannot be reached by `send` on the main
    # object the way a top-level `def` can: `MightyString::String.pop` needs an instance first. This
    # names the class, so a generated dispatcher can do `const_get(klass).new(<ctor args>)` and then
    # call `method` on what it built. Empty for a static method or a language with no such notion,
    # which is why it is optional and why every other generator ignores it.
    klass: str = ""

    def validate(self) -> None:
        if not self.name or not self.module or not self.symbol:
            raise ValueError("package operation fields required")
        if not self.json_safe:
            raise ValueError("package operation is not JSON-safe")


@dataclass(frozen=True)
class PackageContract:
    subject_source: str
    package_name: str
    operations: tuple[PackageOperation, ...]
    dependencies: tuple[str, ...] = ()
    provenance: Provenance = field(default_factory=lambda: Provenance("", ""))

    def validate(self) -> None:
        if not self.subject_source or not self.package_name:
            raise ValueError("package identity required")
        if len(self.operations) < 4:
            raise ValueError("package needs at least four operations")
        for operation in self.operations:
            operation.validate()
        Contract("package", self.provenance).validate()

    def to_json(self) -> dict:
        self.validate()
        return {"subject_source": self.subject_source, "package_name": self.package_name,
                "operations": [{"name": o.name, "module": o.module, "symbol": o.symbol,
                                "signature": o.signature, "json_safe": o.json_safe}
                               for o in self.operations],
                "dependencies": list(self.dependencies), "provenance": self.provenance.to_json()}


@dataclass(frozen=True)
class CheckoutContract:
    """A checkout-native task target and its real execution workload."""

    root: str
    target_paths: tuple[str, ...]
    kind: str = "module"
    build: tuple[tuple[str, ...], ...] = ()
    verify: tuple[tuple[str, ...], ...] = ()
    benchmark: tuple[tuple[str, ...], ...] = ()
    min_speedup: float | None = None
    timing_runs: int = 7
    provenance: Provenance = field(default_factory=lambda: Provenance("", ""))

    def validate(self) -> None:
        Contract(self.kind, self.provenance).validate()
        root = os.path.abspath(self.root)
        if not os.path.isdir(root):
            raise ValueError("checkout root does not exist")
        if not self.target_paths:
            raise ValueError("target paths required")
        for target in self.target_paths:
            path = os.path.abspath(os.path.join(root, target))
            if not path.startswith(root + os.sep) or not os.path.exists(path):
                raise ValueError("invalid target path: %s" % target)
        if not self.verify:
            raise ValueError("verification command required")
        if self.benchmark:
            if self.timing_runs < 3:
                raise ValueError("benchmark needs three timing runs")
            if not all(any("{workspace}" in token for token in command)
                       for command in self.benchmark):
                raise ValueError("benchmark must receive workspace via {workspace}")

    def to_json(self) -> dict:
        self.validate()
        return {"kind": self.kind, "root": self.root, "target_paths": list(self.target_paths),
                "build": [list(x) for x in self.build],
                "verify": [list(x) for x in self.verify],
                "benchmark": [list(x) for x in self.benchmark],
                "min_speedup": self.min_speedup, "timing_runs": self.timing_runs,
                "provenance": self.provenance.to_json()}
