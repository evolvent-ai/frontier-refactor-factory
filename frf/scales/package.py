"""The package scale: a whole public surface, reimplemented.

A package task hands over a library and asks for a faster one behaving identically across its entire
contract -- every entry point, including how each refuses bad input.

WHY PROBES COME FROM A GENERATOR AND NOT A SCHEMA. This is the one real difference from the module
scale, and it is a difference in KIND rather than in degree. A single function has declarable
parameter types. A package's surface has dozens of entry points whose valid inputs have nothing in
common: a stemmer wants real words, a cron parser wants cron expressions, and a serialiser wants
documents with the shapes it claims to support. Expressing that in a declarative schema means
inventing a type language, badly. So a model writes a generator and it runs IN THE CONTAINER, never
on the factory host -- which is the same boundary every other model output crosses.

WHY THE COMPARISON IS STRUCTURAL. A reimplementation returns a dict whose keys were built in a
different order. That is the same answer, and exact byte comparison would call it wrong; JSON
equivalence with a numeric tolerance is what makes "reimplement this in any language" a task that
can actually be passed.

WHY THIS SCALE ALLOWS ANOTHER LANGUAGE AND THE SMALLER ONES DO NOT. A module task is a local change
to code that exists, so it stays in that code's language. A package task is a wholesale rewrite of a
surface, and nothing about the surface requires the original language -- which is why the wire
between the factory and the subject matters here more than anywhere else.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..core import integrity
from ..core.scale import Candidate, Spec
from ..observe import coverage
from ..observe.call import shims
from ..observe.call.runner import Subject

# How many probes a corpus is drawn with. Larger than the module scale's because it has to cover a
# whole surface rather than one function: every entry point needs inputs, and each needs at least
# one that it REFUSES, since error paths are part of a contract.
PROBE_COUNT = 200


@dataclass
class Material:
    """One package, located and ready to be specified."""

    identity: str
    language: str
    root: str                                   # the checkout the reference is built from
    entry_points: tuple                         # the public surface, as names
    description: str
    generator: str = ""                         # model-written; runs only in the container
    target_language: str = ""
    forbidden: tuple = ()
    install: list = field(default_factory=list)


class ProbeSource:
    """Inputs from a generator that ran in the container.

    The generator's OUTPUT is data by the time it reaches here. It is executed on the far side of
    the sandbox boundary and what comes back is a list of argument lists -- so nothing model-written
    is ever executed by the factory process, which is the rule this pipeline does not bend.
    """

    def __init__(self, drawn: list, count: int = PROBE_COUNT) -> None:
        self._drawn = drawn
        self.count = min(count, len(drawn)) if drawn else 0

    def draw(self, count: int) -> list:
        return self._drawn[:count]


class Observer:
    """The call seam, bound to an installed package."""

    def __init__(self, workspace: str, material: Material, *, backend=None) -> None:
        self.workspace = workspace
        self.material = material
        # Which sandbox the subject runs in, so isolation is reported from what is in force.
        self._backend = backend
        self._argv: list = []

    def build(self, spec: Spec) -> None:
        """Write the shim beside the installed package.

        Only the shim, unlike the module scale: there the subject is one file to be copied, and here
        it is a package already installed in the environment, which the shim reaches by name. So
        there is nothing to copy and nothing to compile -- whatever building the package needed
        happened when it was installed.
        """
        shim = shims.load(spec.language)
        os.makedirs(self.workspace, exist_ok=True)
        with open(os.path.join(self.workspace, shim.template), "w", encoding="utf-8") as handle:
            handle.write(shims.source(shim))
        _, self._argv = shim.commands(self.workspace)

    def subject(self, spec: Spec | None = None, *, mutated: bool = False) -> Subject:
        return Subject(self._argv, cwd=self.workspace)

    def coverage(self):
        return coverage.backend_for(self.material.language)

    def forbidden_references(self, spec: Spec) -> list:
        """Where the tree actually reaches the package it is meant to replace.

        This is the check that matters most at this scale: "reimplement this surface" collapses into
        "import the thing you were asked to replace" unless the imports are inspected, and such a
        submission is perfectly correct while having implemented nothing.

        What is returned is what was FOUND, not what is forbidden. Returning the ban list would make
        the evidence check fail every task that has a rule -- which is every task at this scale.
        """
        banned = tuple(self.material.forbidden or (self.material.identity,))
        return [str(hit) for hit in integrity.inspect(self.workspace, banned).hits]

    def isolation(self):
        """How the two sides are kept apart while one is timed -- reported, never assumed."""
        return integrity.isolation_for(self._backend)

    def isolated(self) -> bool:
        return self.isolation().enforced


class Package:
    """The package scale. Four answers; the pipeline does the rest."""

    name = "package"

    def __init__(self, index=None, workspace: str = "", *, observer=None,
                 run_generator=None, backend=None) -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "package")
        self._observer = observer
        # How a generator gets executed. Injected so that this scale never chooses to run
        # model-written code itself: the caller supplies something that runs it in a container, and
        # a caller that supplies something else is making that choice visibly.
        self._run_generator = run_generator
        # Threaded to the Observer so the delegation check reports what is really in force.
        self._backend = backend
        self._material: Material | None = None

    def find(self, budget: int):
        """Candidates from a registry index -- PyPI, npm, crates.io, a reverse-dependency graph.

        All of them can be paged and counted, which is the requirement. A model naming packages it
        remembers cannot say what remains, and a supply whose size is unknown makes a yield a number
        with no denominator.
        """
        if self._index is None:
            raise LookupError(
                "the package scale needs a registry index to source from. Pass one to "
                "Package(index=...), or supply candidates to Factory.build(candidates=[...]).")
        from ..core import sourcing

        return sourcing.walk(self._index, budget)

    def specify(self, candidate: Candidate) -> Spec:
        self._material = self._locate(candidate)
        material = self._material
        return Spec(name=_task_name(material), scale=self.name, language=material.language,
                    description=material.description, build=list(material.install),
                    invoke=["serve"], entry="entry",
                    target_language=material.target_language,
                    environment={"comparison": "structural",
                                 "entry_points": list(material.entry_points),
                                 "forbidden": list(material.forbidden)})

    def observe(self):
        if self._observer is not None:
            return self._observer
        if self._material is None:
            raise RuntimeError("observe() was asked for before specify() chose a package")
        return Observer(self._workspace, self._material, backend=self._backend)

    def probes(self, spec: Spec) -> ProbeSource:
        """Run the generator where model-written code is allowed to run, and take back data."""
        if self._material is None:
            raise RuntimeError("probes() was asked for before specify() chose a package")
        if not self._material.generator:
            raise ValueError("package %s has no probe generator; a surface cannot be sampled from a "
                             "schema, so one is required" % self._material.identity)
        if self._run_generator is None:
            raise RuntimeError(
                "no runner was given for the probe generator. Generators are model-written and must "
                "execute inside a container; Package(run_generator=...) is how that is supplied.")
        drawn = self._run_generator(self._material.generator, PROBE_COUNT)
        return ProbeSource(_as_argument_lists(drawn))

    def _locate(self, candidate: Candidate) -> Material:
        detail = candidate.detail or {}
        if "entry_points" not in detail:
            raise ValueError("candidate %s does not name its public surface; a package task grades "
                             "the whole contract, so the entry points are required"
                             % candidate.identity)
        return Material(
            identity=candidate.identity, language=candidate.language,
            root=detail.get("root", ""), entry_points=tuple(detail["entry_points"]),
            description=detail.get("description", ""), generator=detail.get("generator", ""),
            target_language=detail.get("target_language", ""),
            forbidden=tuple(detail.get("forbidden", ())),
            install=list(detail.get("install", ())))


def _as_argument_lists(drawn) -> list:
    """Whatever the generator produced -> argument lists this pipeline can freeze.

    Validated rather than trusted: a generator is model-written, and one that returns a shape nobody
    expected should fail here, where the message can say so, rather than deep inside a freeze where
    it looks like the subject misbehaved.
    """
    if isinstance(drawn, str):
        drawn = json.loads(drawn)
    if not isinstance(drawn, list):
        raise ValueError("the probe generator returned %s; a list of argument lists is required"
                         % type(drawn).__name__)
    for index, item in enumerate(drawn):
        if not isinstance(item, list):
            raise ValueError("probe %d is %s; every probe must be a list of arguments"
                             % (index, type(item).__name__))
    return drawn


def _task_name(material: Material) -> str:
    stem = material.identity.rsplit("/", 1)[-1].replace("_", "-").lower()
    return "%s-rewrite" % stem if material.target_language else "%s-faster" % stem
