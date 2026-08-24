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
import tempfile

from ..core.scale import Candidate
from .functions import scan

# How long a clone may take. Shallow and single-commit, so this is generous rather than tight.
CLONE_TIMEOUT = 300.0

# How many functions one repository may contribute. The same reasoning as `functions.PER_PACKAGE`:
# a repository with four hundred serviceable functions would otherwise fill a whole batch by
# itself, and a batch drawn from one repository measures that repository rather than the supply.
PER_REPOSITORY = 8


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
                 scale: str = "module") -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "checkouts")
        self._per_repository = per_repository
        self._scale = scale
        # `page()` is an index over FUNCTIONS, not repositories. Keeping the widened candidates
        # means a repository that contributes nothing does not masquerade as end-of-supply to
        # `sourcing.walk()`, which stops on an empty page.
        self._expanded: list[Candidate] = []
        self._source_page = 0
        self._source_exhausted = False

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
            repositories = list(self._index.page(self._source_page, size=4))
            self._source_page += 1
            if not repositories:
                self._source_exhausted = True
                break
            for repository in repositories:
                self._expanded.extend(self._widen(repository))
        start = number * size
        return self._expanded[start:start + size]

    def _widen(self, repository: Candidate) -> list:
        """One repository -> the functions inside it, as candidates."""
        detail = repository.detail or {}
        url = str(detail.get("repository") or "")
        commit = str(detail.get("commit") or "")
        full_name = str(detail.get("identity") or "")
        if not url or not commit:
            # No commit means no version to write down, so an expectation frozen against it would
            # describe whatever the branch pointed at that afternoon.
            return []

        root = self.materialise(url, commit)
        if root is None:
            return []

        stem = full_name.rsplit("/", 1)[-1] or full_name
        found = scan(root, stem, commit)
        if self._scale == "kernel":
            # Kernel is module plus the array vocabulary. Filter before the per-repository
            # cap; slicing first silently discarded later numeric routines whenever a repository
            # sorted scalar helpers ahead of its array functions.
            found = [function for function in found
                     if any(param.get("kind") in ("int_array", "float_array", "complex_array")
                            for param in function.schema.get("params", ()))]
        found = found[:self._per_repository]
        return [_to_candidate(function, root=root, full_name=full_name,
                              commit=commit, scale=self._scale)
                for function in found]

    def materialise(self, url: str, commit: str) -> str | None:
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
                done = subprocess.run(argv, cwd=room, capture_output=True, text=True,
                                      timeout=CLONE_TIMEOUT)
                if done.returncode != 0:
                    shutil.rmtree(room, ignore_errors=True)
                    return None
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(room, ignore_errors=True)
            return None
        return room


def _to_candidate(function, *, root: str, full_name: str, commit: str,
                  scale: str = "module") -> Candidate:
    """One located function -> a Candidate the module scale can specify.

    `detail` carries exactly what `Module._locate` requires, which is the whole point: a repository
    search and a scale that never heard of each other are joined without either learning about the
    other.
    """
    return Candidate(
        identity="github:%s@%s#%s.%s" % (full_name, commit[:12], function.module, function.symbol),
        scale=scale, language="python", source="github-functions",
        detail={
            "source_path": function.path,
            "symbol": function.symbol,
            "schema": function.schema,
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
