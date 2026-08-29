"""Open-world language capability registry.

Language discovery is intentionally broader than adapter certification.  A repository in an
unknown language may still be a valid process task; call-seam scales require a registered adapter.
"""
from __future__ import annotations

from dataclasses import dataclass


CAPABILITY_LEVELS = ("discovered", "repo-capable", "call-capable", "certified")


@dataclass(frozen=True)
class LanguageCapability:
    language: str
    level: str
    adapter: str = ""
    scales: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.level not in CAPABILITY_LEVELS:
            raise ValueError("unknown capability level %r" % self.level)


# WHAT A ROW MEANS. `level` is the rung on the ladder this language's ADAPTER has reached, and
# `scales` are the scales it has reached that rung ON. A scale left out is not a scale that cannot
# run: `capability()` reports an omitted scale one rung lower, which is how "the mechanism works but
# this particular scale has no certified evidence yet" is said. So a row is two claims -- one about
# MECHANISM (is the seam built for this language) and one about EVIDENCE (has it been graded) -- and
# `tests/test_capabilities.py` holds the mechanism half against what the code can actually do.
#
# A CALL SEAM HAS TWO HALVES, and one without the other is not being call-capable. The shim (read a
# JSON line, call the entry, write a reply) ships for eight languages; the function MINER (find the
# callable functions in a source tree) is the other half, and nothing it cannot find ever reaches a
# shim. Declaring a language call-capable on the strength of the shim table alone once made a Go
# module run source hundreds of repositories and widen them into zero candidates, every one refused
# `call-adapter-not-registered:go` -- a yield figure for a supply that does not exist.
#
# THE OPPOSITE STALENESS IS JUST AS REAL, and it is the one that bit us second: when the tree-sitter
# miner landed for go/rust/java/cpp, this table went on saying those languages reached the repo scale
# only. Nothing broke, because `capability()` is read by the attestation and audit records rather
# than by the pipeline -- so the cells they could now produce were reported as cells they could not.
# A registry that lags the mechanism understates the factory instead of overpromising it, which is
# quieter and no less wrong.
_REGISTRY: dict[str, LanguageCapability] = {
    # Repo is omitted deliberately: it has not completed the final Harbor reference-vs-reference
    # audit, so it is reported one rung down rather than inheriting a language-wide `certified`
    # label earned on the call seam.
    "python": LanguageCapability("python", "certified", "python", ("module", "kernel", "package")),
    # Kernel and module are DECLARED because they are attested on disk, not merely mechanical:
    # kernel/javascript, kernel/typescript, module/javascript and module/typescript all carry
    # attested subjects. They were missing here while that evidence already existed.
    "javascript": LanguageCapability("javascript", "call-capable", "javascript",
                                     ("kernel", "module", "package", "repo")),
    "typescript": LanguageCapability("typescript", "call-capable", "typescript",
                                     ("kernel", "module", "package", "repo")),
    # TWO OF THE THREE PARTS, which is not enough and was briefly recorded as though it were. The
    # tree-sitter miner reads these four WITH argument types, and a shim ships for each -- so by the
    # count of two they look ready. They are not: a static shim cannot bind a mined symbol. serve.go
    # demands `func Entry(args []interface{}) (interface{}, error)` in `package main`, and the
    # material is `func CoinChange(coins []int, amount int) int` in `package dynamic`. The first Go
    # kernel batch refused all four candidates at build -- `found packages main (serve.go) and
    # dynamic (subject.go)`, and `undefined: Entry` once that was fixed by hand.
    #
    # What is missing is a per-candidate GENERATED BRIDGE: declare the package and entry the shim
    # expects, unpack the JSON arguments into concrete types, call the real symbol. Until it exists
    # these stay repo-capable, where the process seam needs no bridge because it runs a whole program.
    "go": LanguageCapability("go", "repo-capable", "go", ("repo",),
                             reason="mined symbols need a generated call bridge"),
    "rust": LanguageCapability("rust", "repo-capable", "rust", ("repo",),
                               reason="mined symbols need a generated call bridge"),
    "java": LanguageCapability("java", "repo-capable", "java", ("repo",),
                               reason="mined symbols need a generated call bridge"),
    "cpp": LanguageCapability("cpp", "repo-capable", "cpp", ("repo",),
                              reason="mined symbols need a generated call bridge"),
    # Ruby is missing TWO of the three: no miner (`native_functions._GRAMMARS` has no ruby entry) and
    # no binding (serve.rb splats any arity but hard-codes the name `entry`, and is passed no symbol,
    # so a mined `coin_change` raises NameError). Being dynamic is not the same as binding.
    "ruby": LanguageCapability("ruby", "repo-capable", "ruby", ("repo",),
                               reason="no miner, and serve.rb binds no symbol"),
}


def capability(language: str, *, scale: str = "") -> LanguageCapability:
    """Return known capability, or an explicit discovered-only record for new languages."""
    key = (language or "unknown").strip().lower()
    found = _REGISTRY.get(key)
    if found is None:
        return LanguageCapability(key, "discovered", scales=(), reason="adapter not registered")
    if scale and scale not in found.scales:
        # A registered call adapter remains call-capable even when this particular scale has no
        # certified evidence.  Reporting it as discovered would erase the distinction between an
        # unknown language and a known adapter that is awaiting a scale-specific gate.
        # Use the registry's declared level rather than branching on scale names in core.  This
        # keeps the pipeline language/scale agnostic while still distinguishing a registered call
        # adapter from a repository-only toolchain.
        level = ("call-capable" if found.adapter and found.level in ("call-capable", "certified")
                 else ("repo-capable" if "repo" in found.scales else "discovered"))
        return LanguageCapability(found.language, level, found.adapter, found.scales,
                                  "scale adapter not certified")
    return found


def register(value: LanguageCapability) -> None:
    """Register an adapter without changing core pipeline code."""
    _REGISTRY[value.language.strip().lower()] = value


def snapshot() -> dict[str, dict]:
    return {name: {"language": item.language, "level": item.level, "adapter": item.adapter,
                   "scales": list(item.scales), "reason": item.reason}
            for name, item in sorted(_REGISTRY.items())}
