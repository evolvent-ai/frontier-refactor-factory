"""The mechanical filter: five questions answerable from registry metadata, and nothing else.

DESIGN.md s10 lists what a candidate must look like before anything is cloned:

    a public contract surface     -- there is something to specify and grade
    its own tests                 -- the material's authors already stated its behaviour
    computation rather than I/O   -- a task about network latency has no honest speedup
    a small dependency closure    -- it has to build in a container with no network
    a pinned release tag          -- "the version we measured" has to mean something

EVERY ONE IS DECIDABLE FROM METADATA. No model, no cloning, no judgement. That constraint is not
tidiness -- it is what makes the filter cheap enough to run over a whole page, and what makes two
runs over the same page agree. A filter that asked a model would make `keep` non-deterministic, and
`walk()` records what it rejected into a memory file that a later run trusts.

WHY THE FILTER LIVES HERE AND NOT IN EACH CLIENT. Six registries describe the same five properties
in six vocabularies -- npm has `scripts.test`, RubyGems has a `development` dependency list, crates
has categories. What differs is the EXTRACTION; what must not differ is the decision. So each client
translates its registry's metadata into the small `Facts` record below, and one predicate decides.
Duplicating the decision per client is how two languages end up with different standards for the
same benchmark, and the resulting yield difference looks like a fact about the ecosystem.

WHAT THIS IS NOT. It is not a quality judgement. A package that passes here is one the pipeline is
willing to SPEND a build on; whether it makes a good task is answered later, with evidence, by the
gates. Tightening this filter to raise the yield would be moving a judgement to the one place in
the factory that has no evidence to make it with.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Names that say a package's job is to talk to something. A task built on one measures the network
# or the disk, and its "speedup" is whatever the remote host was doing that afternoon -- which is
# not a property of the submitted code and cannot be graded.
#
# Substring matching on purpose: these appear inside names (`aiohttp-retry`, `django-storages`) far
# more often than they appear alone, and the cost of a false reject is one candidate out of a page.
IO_MARKERS = (
    "http", "https", "requests", "urllib", "aiohttp", "httpx", "fetch", "axios", "grpc", "rpc",
    "socket", "websocket", "tcp", "udp", "smtp", "imap", "ftp", "ssh", "curl", "rest-client",
    "client", "server", "django", "flask", "fastapi", "rails", "express", "boto", "aws-sdk",
    "azure", "gcloud", "google-cloud", "kubernetes", "docker", "database", "sqlalchemy", "psycopg",
    "mysql", "postgres", "sqlite", "mongodb", "redis", "kafka", "rabbitmq", "elasticsearch",
    "selenium", "playwright", "puppeteer", "scrapy", "crawler", "spider", "browser", "cli",
    "logging", "logger", "telemetry", "metrics", "sentry", "filesystem", "fs-extra", "watchdog",
)

# Names that say a package computes. Not required -- a package is not rejected for missing one --
# but a client may use `looks_computational` to RANK, which is allowed: ranking an existing list is
# explicitly permitted by the sourcing rule, unlike producing one.
COMPUTE_MARKERS = (
    "algorithm", "sort", "search", "hash", "crypt", "cipher", "digest", "checksum", "compress",
    "encode", "decode", "codec", "parse", "parser", "lexer", "tokenize", "regex", "matrix",
    "vector", "linalg", "numeric", "numerical", "math", "statistics", "stats", "geometry",
    "graph", "tree", "trie", "heap", "bitset", "interpolate", "fft", "signal", "distance",
    "levenshtein", "fuzzy", "similarity", "diff", "序", "solver", "optimi", "simd", "itertools",
    "functional", "immutable", "collection", "datastructure", "data-structure", "serialize",
    "json", "yaml", "toml", "csv", "template", "format", "color", "date", "time-parse", "unit",
)

# A dependency closure this size or smaller is one an offline container can plausibly install. The
# number is a judgement about container build time, not about quality: every dependency is another
# wheel to vendor and another thing that can fail to build with no network.
MAX_DEPENDENCIES = 12

# What a pinned release looks like. Deliberately loose -- `1.2.3`, `v1.2.3`, `2.0`, `1.2.3-rc1`,
# `3.3.12.0` (RubyGems uses four) all count. What does NOT count is the absence of a version, or a
# placeholder like `0.0.0-<timestamp>-<hash>`, which Go's proxy emits for a module that has never
# tagged a release. That last case is the reason this is a regex and not `bool(version)`.
_RELEASE = re.compile(r"^v?\d+(\.\d+)*([.\-+][A-Za-z0-9.\-+]+)?$")

# Go's pseudo-version: a synthesised stamp for an untagged commit. It is a version string but not a
# release, and treating it as one would pin a task to a commit its own authors never blessed.
_PSEUDO = re.compile(r"^v?0\.0\.0-\d{14}-[0-9a-f]{12}$|^.*-\d{14}-[0-9a-f]{12}$")

# A full commit hash, which is what pins a REPOSITORY. Repositories have no releases -- a repo-scale
# candidate is a clone at a revision, and a revision is the strongest pin there is: stronger than a
# tag, which can be moved. It is listed separately from _RELEASE rather than folded into it because
# the two answer different questions. For a published package an untagged commit is material its
# authors never released, and _PSEUDO rejects it for exactly that reason; for a repository there is
# nothing to release, so the same string is the best possible answer instead of the worst.
_REVISION = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

# Yanked/deleted markers vary by registry; each client normalises into Facts.yanked.


@dataclass(frozen=True)
class Facts:
    """What a registry said, in a vocabulary the filter can decide on.

    One record per candidate, built by the client that fetched it. Every field is something at
    least one of the six registries publishes; a registry that does not publish one leaves it at
    its default, and `unknown_ok` below governs what that costs.
    """

    name: str = ""
    version: str = ""
    summary: str = ""
    keywords: tuple = ()
    dependencies: int | None = None        # runtime deps; None when the registry will not say
    has_tests: bool | None = None          # None when the registry does not expose it
    repository: str = ""                   # where the source is, when published
    yanked: bool = False
    documented: bool = False               # a docs URL, a description, or classifiers
    extra: dict = field(default_factory=dict)

    @property
    def haystack(self) -> str:
        """Everything textual, lowered, for the substring tests. Built once."""
        return " ".join((self.name, self.summary, " ".join(self.keywords))).lower()


def has_contract_surface(facts: Facts) -> bool:
    """Is there something to specify and grade?

    Approximated by "somebody described it": a name, and either a summary or published docs. A
    package with no description at all cannot be turned into a statement a solver could read, and
    the specify stage would be inventing the contract rather than reading it.
    """
    return bool(facts.name) and (len(facts.summary.strip()) >= 16 or facts.documented)


def has_own_tests(facts: Facts) -> bool:
    """Did its authors state its behaviour?

    None means the registry does not publish this, and None is accepted -- rejecting on it would
    exclude entire ecosystems (crates.io and Maven publish no test metadata at all) for a reason
    that is about the registry rather than about the package. The pipeline's own gates measure
    behaviour by running it; this is a cheap prior, not the check.
    """
    return facts.has_tests is not False


def is_computational(facts: Facts) -> bool:
    """Is the work in the process, or on the far side of a socket?

    A speedup measured on a package that spends its time in `recv` is a measurement of somebody
    else's server. There is no way to grade that: the same submission scores differently depending
    on the weather, and no amount of repeated timing fixes it.
    """
    return not any(marker in facts.haystack for marker in IO_MARKERS)


def looks_computational(facts: Facts) -> bool:
    """A positive signal, for RANKING only. Never a rejection.

    Ranking a list that already exists is explicitly allowed; this is the mechanical half of it, so
    that a client can put the likely material first without a model and without dropping anything.
    """
    return any(marker in facts.haystack for marker in COMPUTE_MARKERS)


def has_small_closure(facts: Facts) -> bool:
    """Will it install in a container with no network?

    None -- the registry does not publish a dependency list -- is accepted for the same reason as
    tests. The build stage finds out for certain and reports it as material's fault, which is the
    honest place for that answer.
    """
    return facts.dependencies is None or facts.dependencies <= MAX_DEPENDENCIES


def has_pinned_release(facts: Facts) -> bool:
    """Can "the version we measured" be written down?

    The one filter with no room for unknown. Without a resolvable release the task is pinned to
    whatever the registry served that afternoon, and a solver who installs the package a week later
    gets different code than the expectation was frozen against.
    """
    version = (facts.version or "").strip()
    if not version or facts.yanked:
        return False
    if _REVISION.match(version):
        return True
    if _PSEUDO.match(version):
        return False
    return bool(_RELEASE.match(version))


# The five, in the order DESIGN.md s10 lists them. A tuple rather than a chain of `and` so that a
# rejection can say WHICH one refused -- a filter that only answers False teaches nothing about the
# supply, and "why did the yield drop" is the question this factory most often has to answer.
CHECKS = (
    ("contract surface", has_contract_surface),
    ("own tests", has_own_tests),
    ("computational", is_computational),
    ("small closure", has_small_closure),
    ("pinned release", has_pinned_release),
)


def refusals(facts: Facts) -> list[str]:
    """Which checks said no. Empty means the candidate is worth spending a build on."""
    return [name for name, check in CHECKS if not check(facts)]


def accepts(facts: Facts) -> bool:
    """The predicate itself."""
    return not refusals(facts)


def keep(candidate) -> bool:
    """`walk()`'s `keep`, for candidates a client in this package produced.

    Reads the Facts the client recorded in `detail["facts"]`. A candidate from somewhere else, with
    no facts attached, is KEPT -- this filter refuses material it has evidence against, and absence
    of metadata is not evidence. Silently dropping candidates a foreign index produced would make
    this predicate a second, invisible source of yield loss.
    """
    raw = (getattr(candidate, "detail", None) or {}).get("facts")
    if not isinstance(raw, dict):
        return True
    return accepts(Facts(
        name=str(raw.get("name", "")), version=str(raw.get("version", "")),
        summary=str(raw.get("summary", "")), keywords=tuple(raw.get("keywords", ())),
        dependencies=raw.get("dependencies"), has_tests=raw.get("has_tests"),
        repository=str(raw.get("repository", "")), yanked=bool(raw.get("yanked", False)),
        documented=bool(raw.get("documented", False)),
        extra=dict(raw.get("extra") or {})))


def as_json(facts: Facts) -> dict:
    """Facts -> what travels in a Candidate's detail.

    Recorded rather than recomputed so that a rejection is auditable months later: the metadata the
    decision was made on is in the task's provenance, and a registry that changed a field cannot
    quietly rewrite history.
    """
    return {"name": facts.name, "version": facts.version, "summary": facts.summary,
            "keywords": list(facts.keywords), "dependencies": facts.dependencies,
            "has_tests": facts.has_tests, "repository": facts.repository,
            "yanked": facts.yanked, "documented": facts.documented,
            # Whatever the registry publishes that only it has -- Maven's packaging, npm's engines.
            # Carried through rather than dropped: a field that a client sets and the round trip
            # loses is worse than no field, because a check written against it would read an empty
            # dict and quietly pass everything.
            "extra": dict(facts.extra)}
