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
    "python": LanguageCapability("python", "certified", "python", ("module", "kernel", "package", "repo")),
    "javascript": LanguageCapability("javascript", "call-capable", "javascript", ("package", "repo")),
    "typescript": LanguageCapability("typescript", "call-capable", "typescript", ("package", "repo")),
    "go": LanguageCapability("go", "repo-capable", "go", ("repo",)),
    "rust": LanguageCapability("rust", "repo-capable", "rust", ("repo",)),
    "java": LanguageCapability("java", "repo-capable", "java", ("repo",)),
    "ruby": LanguageCapability("ruby", "repo-capable", "ruby", ("repo",)),
    "cpp": LanguageCapability("cpp", "repo-capable", "cpp", ("repo",)),
}


def capability(language: str, *, scale: str = "") -> LanguageCapability:
    """Return known capability, or an explicit discovered-only record for new languages."""
    key = (language or "unknown").strip().lower()
    found = _REGISTRY.get(key)
    if found is None:
        return LanguageCapability(key, "discovered", scales=(), reason="adapter not registered")
    if scale and scale not in found.scales:
        return LanguageCapability(found.language, "repo-capable" if "repo" in found.scales else "discovered",
                                  found.adapter, found.scales, "scale adapter not certified")
    return found


def register(value: LanguageCapability) -> None:
    """Register an adapter without changing core pipeline code."""
    _REGISTRY[value.language.strip().lower()] = value


def snapshot() -> dict[str, dict]:
    return {name: {"language": item.language, "level": item.level, "adapter": item.adapter,
                   "scales": list(item.scales), "reason": item.reason}
            for name, item in sorted(_REGISTRY.items())}
