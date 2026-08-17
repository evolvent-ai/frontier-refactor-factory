"""Maven Central, for the package scale.

A SOLR INDEX, WHICH IS THE FRIENDLIEST THING HERE. `start` and `rows` are offsets into a result set
and `numFound` is a real count, so paging is arithmetic and `total()` is not a guess. Verified live:
`start=0`, `start=1000` and `start=8000` against the same query all answer 200 with the same
`numFound`.

WHAT MAVEN DOES NOT PUBLISH, and it is more than the others. The search documents carry a group, an
artifact, a latest version, a packaging type and a file-extension list -- and no description, no
dependency list, no keywords. Three of the five mechanical filters have nothing to read.

The choice that follows is the interesting one. The dependency list is in the POM, which is a
separate XML fetch per artifact, and the description is there too. Fetching it turns one page into
one + N requests against repo1.maven.org, which is a different host with different tolerance. So
hydration is OPT-IN, and with it off, `dependencies` is None and the closure filter abstains rather
than guessing -- `filters.has_small_closure` accepts None precisely so that an ecosystem whose
registry is quiet is not excluded for its registry's quietness.

WHAT IS NOT DONE: parsing the POM's `<dependencies>` with a full XML toolchain and resolving
`${property}` placeholders and parent inheritance. That is a Maven resolver, and writing half of one
produces counts that are wrong in a direction nobody can predict. With hydration on, this counts the
direct `<dependency>` elements it can see and says so; anything more honest requires actually
resolving, which belongs in the container that builds the thing.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..core.scale import Candidate
from . import filters
from .http import Http, NotFound

SEARCH = "https://search.maven.org/solrsearch/select?q=%s&start=%d&rows=%d&wt=json"
POM = "https://repo1.maven.org/maven2/%s/%s/%s/%s-%s.pom"

LANGUAGE = "java"

# Solr's row cap for this endpoint. Larger requests are truncated silently.
MAX_ROWS = 200

# Packaging types that are a library with a contract surface. A `pom` artifact is a dependency
# aggregator with no code in it, and a `war` is a deployable application -- neither can be a subject.
LIBRARY_PACKAGING = ("jar", "bundle")


class MavenCentral:
    """One Maven Central search scope, paged by Solr offsets."""

    name = "maven-central"

    def __init__(self, http: Http | None = None, *, query: str = "tags:algorithm",
                 scale: str = "package", hydrate: bool = False) -> None:
        self._http = http or Http()
        self._query = query
        self._scale = scale
        self._hydrate = hydrate
        self._total: int | None = None

    def total(self) -> int | None:
        """Solr's `numFound` for this query. A real count of the result set, not of the repository."""
        if self._total is None:
            self._fetch(0, 1)
        return self._total

    def page(self, number: int, *, size: int = 20):
        size = min(size, MAX_ROWS)
        rows = self._fetch(number * size, size)
        if not rows:
            return []
        return [to_candidate(row, scale=self._scale, source=self.name,
                             dependencies=self._dependencies(row)) for row in rows]

    def _fetch(self, start: int, rows: int) -> list:
        payload = self._http.json(SEARCH % (quote(self._query, safe=":*"), start, rows))
        response = payload.get("response") or {}
        found = response.get("numFound")
        if isinstance(found, int):
            self._total = found
        return list(response.get("docs") or ())

    def _dependencies(self, row: dict) -> int | None:
        """Direct `<dependency>` elements in the POM, or None when not hydrating.

        Counted with a regex rather than an XML parse, and that is a deliberate limit rather than a
        shortcut: what a correct count needs is property resolution and parent-POM inheritance --
        a resolver. This counts what is literally declared and abstains loudly when the POM cannot
        be reached, which keeps the number's meaning narrow enough to state.
        """
        if not self._hydrate:
            return None
        group = str(row.get("g") or "")
        artifact = str(row.get("a") or "")
        version = str(row.get("latestVersion") or row.get("v") or "")
        if not (group and artifact and version):
            return None
        url = POM % (quote(group.replace(".", "/")), quote(artifact), quote(version),
                     quote(artifact), quote(version))
        try:
            pom = self._http.get(url).decode("utf-8", "replace")
        except NotFound:
            return None
        # Test-scoped dependencies are the artifact's own test harness, not its closure.
        blocks = re.findall(r"<dependency>(.*?)</dependency>", pom, re.S)
        return len([b for b in blocks if "<scope>test</scope>" not in b.replace(" ", "")])


def to_candidate(row: dict, *, scale: str = "package", source: str = "maven-central",
                 dependencies: int | None = None) -> Candidate:
    """A Solr document -> a Candidate."""
    group = str(row.get("g") or "")
    artifact = str(row.get("a") or "")
    version = str(row.get("latestVersion") or row.get("v") or "")
    coordinate = "%s:%s" % (group, artifact)
    facts = _facts(row, group, artifact, version, dependencies)
    return Candidate(
        identity="maven:%s:%s" % (coordinate, version),
        scale=scale, language=LANGUAGE, source=source,
        detail={
            "package": coordinate, "group": group, "artifact": artifact, "version": version,
            # Carried in `detail` and not only in `facts`: `filters.as_json` records the five
            # mechanical filters' inputs, and packaging is not one of them -- it is Maven's own
            # library/aggregator distinction, read by `is_library` below.
            "packaging": str(row.get("p") or ""),
            "description": "the Maven artifact %s at %s" % (coordinate, version),
            "entry_points": [], "root": "",
            "install": ["mvn", "dependency:get", "-Dartifact=%s:%s" % (coordinate, version)],
            # The group prefix as well as the coordinate: a reimplementation that imports
            # `com.example.thing` has imported the thing it was asked to replace, and the coordinate
            # string alone would never appear in Java source.
            "forbidden": [coordinate, group],
            "facts": filters.as_json(facts),
        })


def _facts(row: dict, group: str, artifact: str, version: str,
           dependencies: int | None) -> filters.Facts:
    extensions = [str(e) for e in (row.get("ec") or ())]
    return filters.Facts(
        name="%s:%s" % (group, artifact),
        version=version,
        # Maven publishes no description in search results. The coordinate is what there is, and it
        # is genuinely descriptive in this ecosystem -- reverse-DNS group plus an artifact name.
        summary=("%s %s" % (group.replace(".", " "), artifact.replace("-", " "))).strip(),
        keywords=tuple(group.split(".") + artifact.split("-")),
        dependencies=dependencies,
        has_tests=None,
        repository="",
        # A sources jar means the source is fetchable without a clone, which is what the package
        # scale needs to build a reference. Not required, but it is real published evidence.
        documented="-sources.jar" in extensions or "-javadoc.jar" in extensions,
        extra={"packaging": str(row.get("p") or ""),
               "versions": row.get("versionCount")})


def is_library(candidate: Candidate) -> bool:
    """Whether the artifact is a library rather than an aggregator or a deployable.

    Separate from the five mechanical filters because it is Maven-specific: no other registry here
    ships artifacts whose packaging says "this contains no code". Compose it with `filters.keep`
    when sourcing from this index.
    """
    packaging = (candidate.detail or {}).get("packaging", "")
    # An unstated packaging defaults to `jar`, which is Maven's own default -- so an artifact whose
    # document omitted the field is kept rather than dropped. This filter refuses what it has
    # evidence against; missing metadata is not evidence.
    return str(packaging or "jar").lower() in LIBRARY_PACKAGING
