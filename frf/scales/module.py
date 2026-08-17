"""The module scale: one function, called over the wire.

The smallest instance of the call seam, and the one the other two are built from. A module task
hands the solver a function that works and asks for a faster one with identical behaviour.

WHAT THIS FILE IS ALLOWED TO CONTAIN. Four answers and nothing else -- where material comes from,
what to build, which seam, where probes come from. Every stage is shared, and if this file ever
needs to know how a freeze works or what an evidence check does, the abstraction is wrong rather
than this file being special.

WHY THE SUBJECT IS SERVED RATHER THAN IMPORTED. Even here, where the subject is Python and the
factory is Python, the subject runs as a separate process behind a JSON wire. Importing it would be
simpler and would quietly assume the candidate is Python too -- which is the assumption that turns
"any language" into "the language we happened to write a loader for". It is also the precondition
for the two anti-circumvention measures: you cannot inspect the imports of a candidate you have
imported into yourself, and you cannot suspend a process you are inside.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from ..core.scale import Candidate, Spec
from ..observe import coverage
from ..observe.call import shims
from ..observe.call.runner import Subject
from ..observe.probes.schema import Schema, sample

# How many probes a corpus is drawn with. Above the pipeline's floor by a margin: on this seam a
# probe is worth one point, three are held out for timing, and a corpus that only just clears the
# floor before those are removed does not clear it afterwards.
PROBE_COUNT = 60

# The sizes each probe is drawn at. More than one because a candidate that got fast at one
# convenient size has not made the subject faster, and the timing pass scores the worst of them.
SHAPES = ({"n": 32}, {"n": 256}, {"n": 1024})


@dataclass
class Material:
    """One function, located and ready to be specified.

    `schema` is what makes this scale possible at all: a single function has declarable parameter
    types, which is why probes here are SAMPLED. A package's surface has dozens of entry points
    whose valid inputs have nothing in common, and that is why it uses generators instead.
    """

    identity: str
    language: str
    source_path: str
    symbol: str
    description: str
    schema: Schema
    forbidden: tuple = ()


class ProbeSource:
    """Schema sampling, at several shapes, deterministically.

    Seeded from the probe's index rather than from the clock. An Expectation is only worth freezing
    if the probe that produced it can be produced again, on another machine, months later.
    """

    def __init__(self, schema: Schema, count: int = PROBE_COUNT, shapes: tuple = SHAPES) -> None:
        self.schema = schema
        self.count = count
        self.shapes = shapes

    def draw(self, count: int) -> list:
        return [sample(self.schema, seed=i, shape=self.shapes[i % len(self.shapes)])
                for i in range(count)]


class Observer:
    """The call seam, bound to a workspace where the subject is served.

    Everything a shared stage needs from a seam is here and is deliberately small: start the
    subject, measure its coverage, list what it must not reach, and say whether timing is isolated.
    """

    def __init__(self, workspace: str, material: Material) -> None:
        self.workspace = workspace
        self.material = material
        self._argv: list = []

    def build(self, spec: Spec) -> None:
        """Put the subject and its shim where they can be served from."""
        os.makedirs(self.workspace, exist_ok=True)
        shutil.copyfile(self.material.source_path, os.path.join(self.workspace, "subject.py"))

        source, argv = shims.load(spec.language)
        entry = os.path.join(self.workspace, "serve%s" % os.path.splitext(argv[-1])[1] or ".py")
        with open(entry, "w", encoding="utf-8") as handle:
            handle.write(source)
        self._argv = [part.format(entry=entry) for part in argv]

    def subject(self, spec: Spec | None = None, *, mutated: bool = False) -> Subject:
        return Subject(self._argv, cwd=self.workspace)

    def coverage(self):
        return coverage.backend_for(self.material.language)

    def forbidden_references(self, spec: Spec) -> list:
        """What a submission may not reach. Checked mechanically, never by judgement.

        The rule comes from the task and the same submission gets the same verdict every time, which
        is what keeps this an audit rather than an opinion.
        """
        return list(self.material.forbidden)

    def isolated(self) -> bool:
        """Whether timing runs with the two sides separated.

        Answered honestly rather than optimistically: with this False the delegation check reports
        INCONCLUSIVE, which is the correct verdict when work handed to another process would be
        invisible to the clock.
        """
        return True


class Module:
    """The module scale. Four answers; the pipeline does the rest."""

    name = "module"

    def __init__(self, index=None, workspace: str = "", *, observer=None) -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "module")
        self._observer = observer
        self._material: Material | None = None

    # ------------------------------------------------------------------ the four answers
    def find(self, budget: int):
        """Candidates, from an enumerable index.

        No index, no candidates -- and that is the rule rather than an inconvenience. A scale that
        could fall back to asking a model for names would be a scale whose remaining supply is
        unknowable, and an unknowable supply makes a yield meaningless.
        """
        if self._index is None:
            raise LookupError(
                "the module scale needs an index to source from. Pass one to Module(index=...), or "
                "supply candidates directly to Factory.build(candidates=[...]).")
        from ..core import sourcing

        return sourcing.walk(self._index, budget)

    def specify(self, candidate: Candidate) -> Spec:
        """One candidate -> what to build and how to call it."""
        self._material = self._locate(candidate)
        material = self._material
        return Spec(name=_task_name(material), scale=self.name, language=material.language,
                    description=material.description,
                    invoke=["serve", material.symbol], entry=material.symbol,
                    environment={"subject_path": os.path.join(self._workspace, "subject.py"),
                                 "forbidden": list(material.forbidden)})

    def observe(self):
        if self._observer is not None:
            return self._observer
        if self._material is None:
            raise RuntimeError("observe() was asked for before specify() chose a subject")
        return Observer(self._workspace, self._material)

    def probes(self, spec: Spec) -> ProbeSource:
        if self._material is None:
            raise RuntimeError("probes() was asked for before specify() chose a subject")
        return ProbeSource(self._material.schema)

    # ------------------------------------------------------------------ internals
    def _locate(self, candidate: Candidate) -> Material:
        """Turn a candidate into a located function.

        The detail an index supplies is trusted only as far as being VALIDATED here: a schema that
        cannot be parsed raises at this point rather than producing a corpus of the wrong shape,
        which a freeze would happily record.
        """
        detail = candidate.detail or {}
        missing = [k for k in ("source_path", "symbol", "schema") if k not in detail]
        if missing:
            raise ValueError("candidate %s is missing %s; an index must supply enough to call the "
                             "subject" % (candidate.identity, ", ".join(missing)))
        return Material(
            identity=candidate.identity, language=candidate.language,
            source_path=detail["source_path"], symbol=detail["symbol"],
            description=detail.get("description", ""),
            schema=Schema.from_json(detail["schema"]),
            forbidden=tuple(detail.get("forbidden", ())))


def _task_name(material: Material) -> str:
    """A stable, readable name. Derived from the symbol rather than from a counter, so that two runs
    over the same material produce the same task name and a report stays comparable."""
    return material.symbol.replace("_", "-").replace(".", "-").lower()
