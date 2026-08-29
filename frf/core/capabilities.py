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
    # GO HAS ALL THREE, and it is the language the bridge was built for and proved on. Both claims are
    # backed: MECHANISM by `observe/call.servable`, EVIDENCE by an attested kernel/go subject --
    # go-Knapsack, `func Knapsack(maxWeight int, weights, values []int) int` mined from a real
    # repository, bridged, built in E2B, frozen over 57 probes and replayed out of the emitted package.
    #
    # `package` is absent: that scale fans one entry point out to many symbols and needs a generated
    # static dispatcher, which `observe/call/dispatch.py` does not have for Go and refuses loudly
    # rather than guessing.
    "go": LanguageCapability("go", "call-capable", "go", ("kernel", "module", "repo")),
    # ALL THREE PARTS EXIST FOR THESE THREE, each needing a differently shaped bridge -- and that is a
    # claim about MECHANISM, which is the rung `call-capable` names. It is deliberately NOT the claim
    # `certified` makes: unlike go, which carries an attested subject, these have been proven against
    # real toolchains (rustc 1.83, javac 21, g++ 13.3) but have produced no graded task yet. A batch
    # promotes them further; nothing here should.
    #
    # Each bridge lives INSIDE the subject file rather than beside it, for three different reasons:
    # rustc is handed only the shim and reaches the subject as `mod subject`; Serve.java reflects for
    # `Class.forName("Subject")`, so the generated class must be that class; serve.c is compiled as C
    # and its JSON reader is `static`, so the C++ bridge carries a reader of its own.
    "rust": LanguageCapability("rust", "call-capable", "rust", ("kernel", "module", "repo")),
    "java": LanguageCapability("java", "call-capable", "java", ("kernel", "module", "repo")),
    "cpp": LanguageCapability("cpp", "call-capable", "cpp", ("kernel", "module", "repo")),
    # RUBY HAS ALL THREE, and needed no bridge for the third -- only the symbol. serve.rb splatted any
    # arity from the start but hard-coded the name `entry`, which was true of the file and not of the
    # language: a dynamic runtime resolves a name, so `send(ENTRY, *args)` and one argv slot replaced
    # what would have been a generated bridge. Its reader is its own, because Ruby writes no parameter
    # types at all: they come from an `@param` comment, and the grammar is read for STRUCTURE -- only a
    # top-level `def` is reachable by that `send`.
    #
    # `package` IS PRESENT NOW, and for the same reason the bridge was unnecessary: a package
    # dispatcher only has to reach a name, and `send(symbol.to_sym, *args)` after a `require_relative`
    # does that without knowing a single type. The four static languages still lack one because theirs
    # needs a concrete type per argument, which `source/package_adapters.py` does not yet emit -- it
    # captures a signature as a regex STRING.
    #
    # THIS IS NOT A CLAIM ABOUT SUPPLY. Ruby's kernel and module cells stay thin for a reason that is
    # a fact about Ruby rather than about its reader: 1884 definitions across the checkouts on hand
    # carried 4 type annotations between them, and an untyped parameter is refused rather than guessed
    # at. The package scale is indifferent to that, because it dispatches by name.
    "ruby": LanguageCapability("ruby", "call-capable", "ruby",
                               ("kernel", "module", "package", "repo")),
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
