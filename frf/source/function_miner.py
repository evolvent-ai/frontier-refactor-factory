"""Functions inside GitHub repositories, which is what the module and kernel scales source from.

`functions.py` widens a PACKAGE INDEX into the functions inside each package. This widens a
REPOSITORY SEARCH into the same thing, and the difference is only where the source tree comes
from: a released sdist there, a pinned checkout here.

WHY BOTH EXIST RATHER THAN ONE. A registry publishes releases, and a release is a version that
can be written down -- which is what makes an expectation mean the same thing next month. But a
registry is also a language: PyPI is Python, crates.io is Rust, and sourcing only from registries
means the supply is partitioned by publishing convention rather than by what the code is. GitHub
is not partitioned that way, and a repository search filtered by `language:` reaches material no
registry lists at all.

WHAT IS SHARED, AND IT IS THE PART THAT MATTERS. The parsing -- which functions can be served,
what their parameters are, which ones to refuse -- is `functions.scan`, unchanged and uncopied.
That function takes a directory and knows nothing about where it came from, so widening a new
source into functions is a matter of producing a directory. Everything downstream of `scan` is
therefore identical between the two, including every refusal reason.

WHAT IS NOT SHARED: the IDENTITY. `functions.Function.identity` says `pypi:name@version#mod.sym`,
and a checkout has no package name and no version -- it has a repository and a commit. Reusing
that identity would make two functions from different repositories collide in the memory of what
has already been tried, so the identity is built here instead.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import ast
from .javascript_functions import scan as scan_javascript
from . import native_functions as scan_native
from . import ruby_functions as scan_ruby

from ..core.scale import Candidate
from .functions import scan

# What GitHub calls a language, spelled the way the rest of the factory does. GitHub reports `C++`
# and some repositories say `golang`, and a spelling miss here is indistinguishable from an
# unsupported language.
#
# APPLIED TO THE CANDIDATE, not only to the choice of scanner. It used to be consulted for the
# scanner alone, so a C++ candidate was mined correctly and then carried `language="c++"` into a
# pipeline where every table is keyed `cpp`: `shims.load` raised `no shim for 'c++'` at specify --
# after the checkout had been paid for -- and `capability("c++")` would have recorded a registered
# language as `discovered`. One spelling, decided once, at the boundary where the outside world's
# name enters.
_ALIASES = {
    "c++": "cpp", "cplusplus": "cpp", "golang": "go", "rustlang": "rust",
    "c#": "csharp", "objective-c": "objc", "f#": "fsharp",
}


def canonical(language: str) -> str:
    """The name this factory uses for `language`, whatever the source called it."""
    key = (language or "").strip().lower()
    return _ALIASES.get(key, key)

# How long a clone may take. Shallow and single-commit, so this is generous rather than tight.
CLONE_TIMEOUT = 300.0
WIDEN_TIMEOUT = 360.0

# How many functions one repository may contribute. The same reasoning as `functions.PER_PACKAGE`:
# a repository with four hundred serviceable functions would otherwise fill a whole batch by
# itself, and a batch drawn from one repository measures that repository rather than the supply.
PER_REPOSITORY = 8
MAX_REPOSITORY_KB = 100_000


def _function_rank(function, *, kernel: bool) -> tuple:
    """Rank mined functions before the per-repository cap.

    The scanner already proves that inputs are drawable and that work can scale. This ranking adds
    only a preference: array-shaped kernels first, then larger declared inputs, then stable names.
    It never removes a valid scalar module candidate and is independent of repository identity.
    """
    params = (getattr(function, "schema", {}) or {}).get("params", ())
    kinds = {str(p.get("kind", "")) for p in params}
    arrays = {"int_array", "float_array", "complex_array"}
    sized = {"string", "bytes", "int_array", "float_array", "complex_array", "list", "map"}
    name = "%s.%s" % (getattr(function, "module", ""), getattr(function, "symbol", ""))
    non_numeric = ("search", "sort", "combination", "permutation", "path", "graph")
    return (0 if kernel and kinds & arrays and not any(word in name.lower() for word in non_numeric) else 1,
            0 if kinds & sized else 1,
            -len(params), str(getattr(function, "module", "")),
            str(getattr(function, "symbol", "")))


def _worth_probing(function, language: str = "python") -> bool:
    """Cheap, source-agnostic gate before a mined function enters E2B.

    A callable with no declared inputs cannot express an input-dependent speedup; in practice these
    are constants, documentation helpers, or metadata accessors. Private names are similarly poor
    public benchmark subjects. Keep this deliberately conservative: scalar functions remain valid
    module candidates, while adequacy and evidence retain responsibility for deeper quality checks.
    """
    symbol = str(getattr(function, "symbol", ""))
    schema = getattr(function, "schema", {}) or {}
    params = schema.get("params", ()) if isinstance(schema, dict) else ()
    if not params or symbol.startswith("_"):
        return False
    # The module corpus is sampled at up to 1024 elements. These names conventionally denote
    # exponential/backtracking routines whose input size dominates the probe timeout; refusing
    # them before E2B is both cheaper and more honest than discovering it after five freeze runs.
    explosive = ("combination", "permutation", "subset", "backtrack", "fibonacci", "power_set")
    if any(word in symbol.lower() for word in explosive):
        return False
    if "search" in symbol.lower() and any(
            (p.get("kind", "") if isinstance(p, dict) else getattr(p, "kind", ""))
            in {"int_array", "float_array"} for p in params):
        # Search routines conventionally require an ordered domain. If the AST schema did not
        # capture that precondition, random probes are mostly out-of-contract and produce a weak
        # task (as opposed to a useful benchmark), so defer this candidate.
        schema = getattr(function, "schema", {}) or {}
        if not any(bool(p.get("sorted")) for p in schema.get("params", ()) if isinstance(p, dict)):
            return False
    # Avoid obvious state/time/random subjects before paying for E2B freeze. This is deliberately
    # conservative: dynamic nondeterminism is still caught by the five-run freeze gate.
    if language not in {"python", "python3"}:
        return True
    try:
        tree = ast.parse(open(function.path, encoding="utf-8", errors="replace").read())
        imports = {alias.name.split(".", 1)[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # The module seam serves the mined file standalone. Relative imports resolve only
                # inside the original checkout and deterministically fail after emission.
                if node.level:
                    return False
                if node.module:
                    imports.add(node.module.split(".", 1)[0])
        if imports & {"random", "secrets", "time"}:
            return False
    except (OSError, SyntaxError, ValueError):
        return False
    return True


def _scanner(language: str):
    """Which reader can mine `language`, or None if no reader can.

    ONE ANSWER, because two callers must not disagree: `_widen`, which mines, and `supported`,
    which reports whether mining is possible at all. While those were derived separately a reader
    could land with nothing else noticing -- which is exactly what happened when the tree-sitter
    reader arrived and `core/capabilities.py` went on declaring go/rust/java/cpp able to reach the
    repo scale only. The registry was not wrong about evidence; it was stale about mechanism.

    THE PYTHON SCANNER IS NOT A GENERIC SOURCE PARSER. A checkout is only ever handed to the reader
    for its own language: feeding a Go tree to `ast` and labelling the result Python would turn an
    open-world source into a false call candidate. A language with no reader is refused by the
    caller and stays eligible for the process-seam repo scale, which needs no miner.

    The returned callable takes `(root, stem, commit)`, so a caller never needs to know which reader
    answered or that the native one must be told its language.
    """
    key = canonical(language)
    if key in ("python", "python3"):
        return scan
    if key in ("javascript", "typescript"):
        return scan_javascript
    # Ruby is its own reader: it has no parameter types in the language, so the types come from a
    # written `@param`, and reachability has to come from a real parse -- `serve.rb` sends to the main
    # object, so only a top-level `def` can be served.
    if key == "ruby" and scan_ruby.supported():
        return scan_ruby.scan
    if scan_native.supported(key):
        return lambda root, stem, commit: scan_native.scan(root, stem, commit, language=key)
    return None


def supported(language: str) -> bool:
    """Whether any reader can mine `language` -- the call seam's mining half, as a question.

    `core/` may not import a source layer to ask this, so `core/capabilities.py` states its own
    judgement and a test holds that judgement against this answer. The registry decides what the
    EVIDENCE supports; this decides what the MECHANISM supports; a disagreement is a bug in one of
    them and the test says which.
    """
    return _scanner(language) is not None


class GitHubFunctions:
    """Functions inside repositories, sourced from a GitHub repository search.

    Composed with a `GitHub` index rather than replacing one, exactly as `PythonFunctions` is
    composed with `PyPI`: the enumerable thing is still the search, and this widens each result
    into the functions it contains. `total()` is therefore the search's total -- honest, and the
    denominator a yield should be computed against, since the unit of supply really is the
    repository.
    """

    name = "github-functions"

    def __init__(self, index, *, workspace: str = "", per_repository: int = PER_REPOSITORY,
                 scale: str = "module", log=None, widen_timeout: float = WIDEN_TIMEOUT) -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "checkouts")
        self._per_repository = per_repository
        self._scale = scale
        self._log = log or (lambda _message: None)
        self._widen_timeout = widen_timeout
        # `page()` is an index over FUNCTIONS, not repositories. Keeping the widened candidates
        # means a repository that contributes nothing does not masquerade as end-of-supply to
        # `sourcing.walk()`, which stops on an empty page.
        self._expanded: list[Candidate] = []
        self._source_page = 0
        self._source_exhausted = False
        self.rejection_counts: dict[str, int] = {}
        self.repositories_walked = 0
        self.functions_walked = 0

    def total(self) -> int | None:
        return self._index.total()

    def page(self, number: int, *, size: int = 20):
        """One page of repositories, widened into the functions inside them.

        A repository that will not clone, or holds nothing serviceable, contributes NOTHING and is
        not an error: most repositories are like that, and it is the ordinary shape of this supply.
        What would be an error is a page that came back empty because the search misbehaved, and
        that is the inner index's business -- it raises, and the raise travels.
        """
        needed = (number + 1) * size
        while len(self._expanded) < needed and not self._source_exhausted:
            repositories = list(self._index.page(
                self._source_page, size=max(1, min(4, needed - len(self._expanded)))))
            self._source_page += 1
            if not repositories:
                self._source_exhausted = True
                break
            for repository in repositories:
                # A REPOSITORY ALREADY DRAWN FROM IS NOT WORTH MINING AGAIN, and skipping it here is
                # the only place the saving exists: `_widen` clones the repository and parses every
                # file in it, while `sourcing.walk` dedups only on what comes back. With a persisted
                # seen-set that inverts the cost -- a restarted roll spent its whole time
                # re-downloading and re-parsing repositories it had already produced from, and made
                # one attempt in nine minutes.
                #
                # Function identities extend their repository's, so a prefix answers "have we been
                # here". `max_per_repository` already caps how many tasks one repository may
                # contribute, so returning to one is waste even when it still holds functions.
                if _already_drawn_from(repository.identity, getattr(self, "already_seen", None)):
                    self.rejection_counts["already-mined"] = \
                        self.rejection_counts.get("already-mined", 0) + 1
                    continue
                self.repositories_walked += 1
                started = time.monotonic()
                self._log("%s: widening repository %s" % (self.name, repository.identity))
                widened = self._widen(repository)
                self.functions_walked += len(widened)
                self._expanded.extend(widened)
                self._log("%s: widened %s in %.1fs (%d candidate(s), rejections=%s)" %
                          (self.name, repository.identity, time.monotonic() - started,
                           len(self._expanded), self.rejection_counts or {}))
        start = number * size
        return self._expanded[start:start + size]

    def _widen(self, repository: Candidate) -> list:
        """One repository -> the functions inside it, as candidates."""
        deadline = time.monotonic() + self._widen_timeout
        detail = repository.detail or {}
        size_kb = int(detail.get("size_kb") or 0)
        if size_kb > MAX_REPOSITORY_KB:
            self.rejection_counts["repository-too-large"] = self.rejection_counts.get("repository-too-large", 0) + 1
            return []
        url = str(detail.get("repository") or "")
        commit = str(detail.get("commit") or "")
        full_name = str(detail.get("identity") or "")
        if not url or not commit:
            # No commit means no version to write down, so an expectation frozen against it would
            # describe whatever the branch pointed at that afternoon.
            self.rejection_counts["unversioned-repository"] = self.rejection_counts.get("unversioned-repository", 0) + 1
            return []

        root = self.materialise(url, commit, deadline=deadline)
        if root is None:
            self.rejection_counts["checkout-failed"] = self.rejection_counts.get("checkout-failed", 0) + 1
            return []

        stem = full_name.rsplit("/", 1)[-1] or full_name
        # CANONICALISED HERE, at the one point where the outside world's name for a language enters.
        # GitHub says `C++`; every table in this factory is keyed `cpp`. Carrying the outside spelling
        # onwards mined the checkout correctly and then refused it at specify with
        # `no shim for 'c++'` -- a language we support, reported as one we do not, after the clone had
        # been paid for.
        language = canonical(str(getattr(repository, "language", "")
                                 or detail.get("language") or ""))
        # WHICH READER, asked in one place. `_scanner` holds the language-to-reader mapping so that
        # this branch and `supported()` cannot drift apart -- and each reader is only ever given a
        # checkout in its own language, because feeding a Go tree to Python's `ast` and labelling the
        # result Python would turn an open-world source into a false call candidate.
        scanner = _scanner(language)
        if scanner is None:
            # No reader for this language. Our gap, not the material's, and the repository stays
            # eligible for the process-seam repo scale, which needs no miner.
            reason = "call-adapter-not-registered:%s" % (language or "unknown")
            self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
            return []
        found = scanner(root, stem, commit)
        if not found and scan_native.supported(language):
            # A typing reader ran and found nothing it could type. That is a fact about THIS
            # material, and it must not wear the same label as a language we cannot read at all --
            # otherwise a registered adapter is indistinguishable from a missing one.
            reason = "no-typeable-functions:%s" % language
            self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
            return []
        found = [function for function in found if _worth_probing(function, language)]
        if self._scale == "kernel":
            # Older scanners represented explicitly typed list[int]/list[float] parameters as
            # generic lists.  They are still numeric, sized inputs and satisfy the kernel contract;
            # normalize only when the element type is known, never for a bare list.
            for function in found:
                for param in function.schema.get("params", ()):
                    if param.get("kind") == "list":
                        element = (param.get("element") or {}).get("kind")
                        if element in ("int", "float"):
                            param["kind"] = "%s_array" % element
                            param["dtype"] = "int64" if element == "int" else "float64"
        if not found:
            self.rejection_counts["no-drawable-functions"] = self.rejection_counts.get("no-drawable-functions", 0) + 1
        found.sort(key=lambda function: _function_rank(function, kernel=self._scale == "kernel"))
        if self._scale == "kernel":
            # Kernel is module plus the array vocabulary. Filter before the per-repository
            # cap; slicing first silently discarded later numeric routines whenever a repository
            # sorted scalar helpers ahead of its array functions.
            found = [function for function in found
                     if any(param.get("kind") in ("int_array", "float_array", "complex_array")
                            for param in function.schema.get("params", ()))]
        found = found[:self._per_repository]
        return [_to_candidate(function, root=root, full_name=full_name,
                              commit=commit, scale=self._scale, language=language)
                for function in found]

    def materialise(self, url: str, commit: str, *, deadline: float | None = None) -> str | None:
        """A shallow checkout at one commit. -> where it landed, or None.

        PINNED, ALWAYS -- the same rule `source/checkout.py` states: a clone of a branch is a clone
        of whatever the branch pointed at that afternoon, and an expectation frozen against it
        describes a program that no longer exists.
        """
        room = os.path.join(self._workspace, commit[:16])
        if os.path.isdir(os.path.join(room, ".git")):
            return room
        os.makedirs(room, exist_ok=True)
        try:
            for argv in (["git", "init", "--quiet"],
                         ["git", "remote", "add", "origin", url],
                         ["git", "fetch", "--quiet", "--depth", "1", "origin", commit],
                         ["git", "checkout", "--quiet", "FETCH_HEAD"]):
                remaining = CLONE_TIMEOUT if deadline is None else min(
                    CLONE_TIMEOUT, max(0.1, deadline - time.monotonic()))
                done = subprocess.run(argv, cwd=room, capture_output=True, text=True,
                                      timeout=remaining)
                if done.returncode != 0:
                    shutil.rmtree(room, ignore_errors=True)
                    return None
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(room, ignore_errors=True)
            return None
        return room


def _to_candidate(function, *, root: str, full_name: str, commit: str,
                  scale: str = "module", language: str = "python") -> Candidate:
    """One located function -> a Candidate the module scale can specify.

    `detail` carries exactly what `Module._locate` requires, which is the whole point: a repository
    search and a scale that never heard of each other are joined without either learning about the
    other.
    """
    return Candidate(
        identity="github:%s@%s#%s.%s" % (full_name, commit[:12], function.module, function.symbol),
        scale=scale, language=language, source="github-functions",
        detail={
            "source_path": function.path,
            "symbol": function.symbol,
            "schema": function.schema,
            # WHAT A GENERATED BRIDGE NEEDS, and only the static readers set it. A dynamic shim binds
            # a symbol at run time and needs neither; a static one has to be handed source that
            # declares the right package and a variable of the right type to hold the answer. Absent
            # for python/javascript/typescript, so both are read with a default downstream.
            "result": getattr(function, "result", None),
            "declared_package": getattr(function, "declared_package", ""),
            # Which class holds the symbol. Java only: every method there lives in a class, so its
            # bridge calls `Owner.method(...)` and cannot be generated without the name. "" for the
            # languages whose functions stand at file scope.
            "owner": getattr(function, "owner", ""),
            # The whole checkout, so checkout-native emission keeps local imports and fixtures
            # exactly as the repository published them.
            "root": root,
            "target_paths": [os.path.relpath(function.path, root)],
            "description": function.doc.splitlines()[0][:200] if function.doc else
                           "the %s function from %s" % (function.symbol, full_name),
            # What a submission must not reach for. The repository's own name, so the delegation
            # check can look for it without the scale knowing about code search.
            "forbidden": [full_name.rsplit("/", 1)[-1]],
            "package": full_name.rsplit("/", 1)[-1], "version": commit[:12],
            "module": function.module,
        })


def _already_drawn_from(identity: str, spent) -> bool:
    """Whether anything already walked came out of this repository. -> True to skip mining it.

    A function identity is its repository's plus a `#path.symbol` suffix, so the repository is a
    prefix of everything mined from it.
    """
    if not spent:
        return False
    if identity in spent:
        return True
    prefix = identity + "#"
    return any(seen.startswith(prefix) for seen in spent)
