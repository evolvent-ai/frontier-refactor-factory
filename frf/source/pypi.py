"""PyPI, for the package scale.

PAGEABILITY, HONESTLY. PyPI retired its search API; the JSON endpoint answers about ONE project and
there is no "list projects, page 2". What it does publish is the PEP 691 simple index -- every
project name, in one response, in a stable order. So this index is a SNAPSHOT: the name list is
fetched once, and pages are cut from it locally.

That is a real difference from an API that pages server-side, and it is worth being precise about
what is gained and lost. Gained: `total()` is a true count, and page 7 is the same page 7 next week
for the same snapshot, because the list is ordered and paging is arithmetic. Lost: the snapshot is
40MB and takes a few seconds, so a client that only wants one page still pays for all of them --
which is why it is fetched lazily, on the first page, and not in the constructor.

The alternative was to fake it: pick a fixed list of "interesting" package names and page through
that. It would have been faster and it would have been the thing sourcing.py exists to forbid --
a list produced by whoever wrote it, whose remaining size is a property of the author rather than of
PyPI, and which would have made every yield number meaningless.

WHY HYDRATION IS PER PACKAGE. The simple index publishes names and nothing else, and the mechanical
filters need a version, a summary and a dependency count. So each name on a page costs one JSON
request. That is the price of PyPI having no search, and it is why `page(size=)` is small by default.
"""
from __future__ import annotations

from ..core.scale import Candidate
from . import filters
from .http import Http, all_or_nothing, envelope

SIMPLE_INDEX = "https://pypi.org/simple/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
PROJECT = "https://pypi.org/pypi/%s/json"

LANGUAGE = "python"

# Classifiers that say a project publishes tests, and ones that say it is an application rather than
# a library. Read from metadata the project itself declared -- no cloning, no judgement.
_TEST_CLASSIFIERS = ("Topic :: Software Development :: Testing",)
_LIBRARY_CLASSIFIERS = ("Topic :: Software Development :: Libraries",)


class PyPI:
    """Every project on PyPI, paged from one snapshot of the simple index.

    `subset` narrows the snapshot to names matching a substring, which is a FILTER over an
    enumerable list rather than a source of names -- the distinction sourcing.py rests on. With it,
    `total()` still answers truthfully, about the filtered list.
    """

    name = "pypi"

    def __init__(self, http: Http | None = None, *, subset: str = "",
                 scale: str = "package", rank: bool = True) -> None:
        self._http = http or Http()
        self._subset = subset.strip().lower()
        self._scale = scale
        self._rank = rank
        self._names: list[str] | None = None

    def total(self) -> int | None:
        """A real count: the snapshot is a list and its length is its size.

        Not fabricated and not an estimate. It does cost the snapshot fetch, which is why the
        pipeline asks for it once, through `walk()`, rather than per page.
        """
        return len(self._snapshot())

    def page(self, number: int, *, size: int = 20):
        """One page of hydrated candidates.

        An empty return means the snapshot is exhausted -- that is the ONLY thing that produces one.
        A network failure raises; see http.py for why that distinction is load-bearing.
        """
        names = self._snapshot()
        window = names[number * size:(number + 1) * size]
        if not window:
            return []
        return all_or_nothing(window, self._candidate, index=self.name)

    def _snapshot(self) -> list[str]:
        """The name list, fetched once per instance.

        Ordered as PyPI publishes it, then filtered and optionally ranked -- both deterministic, so
        page N is page N again for a later run over the same snapshot. Sorted before ranking because
        PyPI's own order is not documented as stable, and paging requires that it be.
        """
        if self._names is not None:
            return self._names
        payload = self._http.json(SIMPLE_INDEX, accept=SIMPLE_ACCEPT)
        names = sorted(str(p.get("name", "")) for p in envelope(payload, "projects", index=self.name)
                       if p.get("name"))
        if self._subset:
            names = [n for n in names if self._subset in n.lower()]
        if self._rank:
            # Ranking, not producing: the list is the same list, reordered so that names which look
            # computational are hydrated first. Nothing is dropped, so total() is unaffected.
            names.sort(key=lambda n: (not filters.looks_computational(filters.Facts(name=n)), n))
        self._names = names
        return names

    def _candidate(self, project: str) -> Candidate:
        return to_candidate(self._http.json(PROJECT % project), scale=self._scale,
                            source=self.name)


def to_candidate(payload: dict, *, scale: str = "package", source: str = "pypi") -> Candidate:
    """PyPI's JSON -> a Candidate the package scale can locate.

    Split out from the client so the offline tests can exercise the parsing against a recorded
    body -- the half that breaks when a registry renames a field, and the half a skipped live test
    would have left unchecked.
    """
    info = payload.get("info") or {}
    name = str(info.get("name") or "")
    version = str(info.get("version") or "")
    facts = _facts(payload)
    return Candidate(
        # Name AND version. The version is what makes this stable across runs: a task frozen against
        # 1.2.3 is not the same material as one frozen against 1.3.0, and an identity without it
        # would let memory suppress a package that has since changed entirely.
        identity="pypi:%s==%s" % (name.lower(), version),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": name, "version": version,
            "description": str(info.get("summary") or ""),
            # What Package._locate requires. The surface is not knowable from metadata -- it needs
            # the installed module -- so it is left empty and the specify stage fills it in. Naming
            # it here documents that the gap is expected rather than forgotten.
            "entry_points": [],
            "root": "", "install": ["pip", "install", "%s==%s" % (name, version)],
            "forbidden": [name.lower(), name.lower().replace("-", "_")],
            "repository": facts.repository,
            "requires_python": str(info.get("requires_python") or ""),
            "facts": filters.as_json(facts),
        })


def _facts(payload: dict) -> filters.Facts:
    info = payload.get("info") or {}
    classifiers = [str(c) for c in (info.get("classifiers") or ())]
    requires = [str(r) for r in (info.get("requires_dist") or ())]
    urls = info.get("project_urls") or {}
    return filters.Facts(
        name=str(info.get("name") or ""),
        version=str(info.get("version") or ""),
        summary=str(info.get("summary") or ""),
        keywords=tuple(_keywords(info)),
        # Extras excluded: `requires_dist` lists optional dependencies alongside required ones, and
        # counting a package's test extras against its closure would reject libraries for having
        # thorough CI. Only what a plain install pulls counts.
        dependencies=len([r for r in requires if "extra ==" not in r]),
        has_tests=any(c.startswith(_TEST_CLASSIFIERS) for c in classifiers) or None,
        repository=_repository(urls, info),
        yanked=bool(_latest_is_yanked(payload)),
        documented=bool(info.get("docs_url") or urls.get("Documentation")
                        or any(c.startswith(_LIBRARY_CLASSIFIERS) for c in classifiers)))


def _keywords(info: dict) -> list[str]:
    raw = info.get("keywords")
    if isinstance(raw, list):
        return [str(k) for k in raw]
    return [k.strip() for k in str(raw or "").replace(",", " ").split() if k.strip()]


def _repository(urls: dict, info: dict) -> str:
    for key in ("Source", "Source Code", "Repository", "Homepage", "Code"):
        if urls.get(key):
            return str(urls[key])
    return str(info.get("home_page") or "")


def _latest_is_yanked(payload: dict) -> bool:
    """Whether the version this metadata describes was withdrawn.

    Checked because a yanked release cannot be pinned: pip refuses it by default, so a task built on
    one is a task whose install step fails for every solver.
    """
    return any(bool(f.get("yanked")) for f in (payload.get("urls") or ()))
