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


_REGISTRY: dict[str, LanguageCapability] = {
    # Certification is evidence-backed per scale.  Repo has not completed the final Harbor
    # reference-vs-reference audit yet, so it must remain repo-capable rather than inheriting a
    # language-wide certified label from the call seam.
    "python": LanguageCapability("python", "certified", "python", ("module", "kernel", "package")),
    "javascript": LanguageCapability("javascript", "call-capable", "javascript", ("package", "repo")),
    "typescript": LanguageCapability("typescript", "call-capable", "typescript", ("package", "repo")),
    # EVERY SHIMMED LANGUAGE IS CALL-CAPABLE, and the shims table is the single source of truth:
    # nine templates ship (serve.py/js/rs/go/c/rb and Serve.java), each with a toolchain image and
    # a verify command in _LANGUAGE_SETUP. These five used to be declared repo-capable only, with
    # scales=("repo",), which did not match what the code could do: module.py and kernel.py call
    # shims.materialise directly and never consult this registry, so a Go module task was being
    # built while its capability record said the language could not do module scale. Reporting
    # less than what is implemented quietly hides coverage; reporting a scale is a claim, not
    # evidence -- so these stay call-capable (adapter registered) rather than certified, and
    # certification per scale comes from the audit matrix, not from this table.
    "go": LanguageCapability("go", "call-capable", "go", ("module", "kernel", "package", "repo")),
    "rust": LanguageCapability("rust", "call-capable", "rust", ("module", "kernel", "package", "repo")),
    "java": LanguageCapability("java", "call-capable", "java", ("module", "kernel", "package", "repo")),
    "ruby": LanguageCapability("ruby", "call-capable", "ruby", ("module", "kernel", "package", "repo")),
    "cpp": LanguageCapability("cpp", "call-capable", "cpp", ("module", "kernel", "package", "repo")),
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
