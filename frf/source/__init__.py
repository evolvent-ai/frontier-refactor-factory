"""The indexes candidates are sourced from -- one per registry, plus code search.

`core/sourcing.py` states the rule these exist to satisfy: a candidate list must come from an
enumerable index, and a model may rank or filter candidates but never produce them. This package is
what makes that rule affordable, because an index is only useful if someone has written the fifty
lines that page it.

WHAT EVERY CLIENT IN HERE PROMISES.

    page(n, size=...)   the nth page, as Candidates. EMPTY MEANS EXHAUSTED and nothing else does:
                        a network failure raises. That asymmetry is the one thing in this package
                        that must not be got wrong -- `walk()` stops on an empty page, so a client
                        that turned a timeout into `[]` would end a batch early and write a
                        coverage record saying the supply had run out.
    total()             how many exist, or None when the registry will not say. None is honest and
                        supported; a fabricated denominator is not.
    name                what appears in the coverage log.

WHICH REGISTRIES ANSWER WHICH QUESTION HONESTLY. They differ more than one would hope, and the
differences are recorded in each module rather than smoothed over:

    pypi         a full name list, paged locally. A real total.
    npm          server-side paging over a search. A real total for that search.
    crates.io    real paging and a real total; page 1001 answers 400, so `reachable()` says what
                 can actually be walked as distinct from what exists.
    pkg.go.dev   the module proxy's index, walked by publication time.
    maven        server-side paging over a search, with a real total.
    rubygems     paged, but publishes NO total. `total()` returns None and means it.
    github       repository search, capped at 1000 results per query -- so it is SEGMENTED by star
                 range, which is the only way past a wall that low.
    hackage      all Haskell packages, paged locally from the /packages/names list.
    opam         OCaml packages from the opam repository on GitHub.
    hex.pm       Elixir/Erlang packages, real server-side paging.

`filters` holds the mechanical prefilters DESIGN.md s10 lists. They are decidable from registry
metadata alone: no model, no judgement, and each names which check refused so that a fall in yield
can be attributed rather than guessed at.
"""
from __future__ import annotations

from .chain import Chain
from .checkout import Checkout, Repositories
from .crates import Crates
from .filters import Facts, accepts, keep, refusals
from .function_miner import GitHubFunctions
from .github_package import GitHubPackages
from .github import GitHub
from .golang import GoModules
from .hackage import Hackage
from .hex import Hex
from .http import (Http, HttpError, NotFound, RateLimited, SourceError, TransportError,
                   Unauthorized)
from .maven import MavenCentral
from .npm import Npm
from .opam import Opam
from .package_adapters import Operation, operations
from .repo_harvest import (HarvestStats, Harvested, fixture_archive, harvest_corpus,
                           harvest_files, scenarios_from_harvest)
from .repo_survey import RepoSurvey, survey
from .rubygems import RubyGems
from .pypi import PyPI
from .tokens import TokenPool

# Registry name -> the client that walks it. A table so that "which indexes does this installation
# have" is a question with a printable answer, in the same spirit as the shim and coverage tables.
INDEXES = {
    "pypi": PyPI,
    "npm": Npm,
    "crates.io": Crates,
    "pkg.go.dev": GoModules,
    "maven": MavenCentral,
    "rubygems": RubyGems,
    "github": GitHub,
    "hackage": Hackage,
    "opam": Opam,
    "hex.pm": Hex,
    # Not a plain registry: widens a GitHub index into the hot-function FILE inside each
    # repository. Used by the module and kernel scales. See function_miner.py.
    "github-functions": GitHubFunctions,
    "github-packages": GitHubPackages,
}

# Language -> list of source index names that primarily serve candidates in that language.
# A source may appear under multiple languages when it serves more than one (e.g. maven covers
# java/kotlin/scala; hex.pm covers elixir and erlang). GitHub is listed under every language
# because it filters by language via its `language:X` query parameter.
_LANGUAGE_SOURCES: dict[str, list[str]] = {
    "python":       ["pypi", "github"],
    "rust":         ["crates.io", "github"],
    "go":           ["pkg.go.dev", "github"],
    "javascript":   ["npm", "github"],
    "typescript":   ["npm", "github"],
    "java":         ["maven", "github"],
    "kotlin":       ["maven", "github"],
    "scala":        ["maven", "github"],
    "ruby":         ["rubygems", "github"],
    "haskell":      ["hackage", "github"],
    "ocaml":        ["opam", "github"],
    "elixir":       ["hex.pm", "github"],
    "erlang":       ["hex.pm", "github"],
    "cpp":          ["github"],
    "c":            ["github"],
    "swift":        ["github"],
    "crystal":      ["github"],
    "nim":          ["github"],
    "zig":          ["github"],
    "lua":          ["github"],
    "perl":         ["github"],
    "r":            ["github"],
    "julia":        ["github"],
    "fsharp":       ["github"],
    "csharp":       ["github"],
}


def available() -> list[str]:
    """Which indexes this installation can source from."""
    return sorted(INDEXES)


def index_for(name: str):
    """-> the client class for one registry.

    Raises rather than returning None, and names what there is. A caller that mistyped a registry
    would otherwise get an AttributeError somewhere that has forgotten what it asked for.
    """
    key = (name or "").strip().lower()
    if key not in INDEXES:
        raise LookupError("no index called %r; this installation has %s"
                          % (name, ", ".join(available())))
    return INDEXES[key]


def sources_for_language(language: str) -> list:
    """The source index classes that can provide candidates in the given language.

    Returns an empty list for languages with no dedicated index (only GitHub general search).
    The GitHub entry in the returned list is the GitHub class configured for that language; the
    others are bare classes that the caller should instantiate as needed.

    Example:
        for cls in sources_for_language("rust"):
            index = cls() if cls is not GitHub else cls(language="rust")
            ...
    """
    lang = (language or "").strip().lower()
    names = _LANGUAGE_SOURCES.get(lang, ["github"])
    return [INDEXES[n] for n in names if n in INDEXES]


__all__ = ["Chain", "Checkout", "Crates", "Facts", "GitHub", "GitHubFunctions", "GitHubPackages", "GoModules", "Hackage", "HarvestStats", "Harvested", "Hex", "Http", "HttpError", "INDEXES", "MavenCentral", "NotFound", "Npm", "Opam", "Operation", "PackageOperation", "PyPI", "RateLimited", "RepoSurvey", "Repositories", "RubyGems", "SourceError", "TokenPool", "TransportError", "Unauthorized", "accepts", "available", "fixture_archive", "harvest_corpus", "harvest_files", "index_for", "keep", "operations", "refusals", "scenarios_from_harvest", "survey", "sources_for_language"]
