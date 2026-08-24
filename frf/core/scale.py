"""What a scale is, expressed as the four questions the pipeline asks it.

The pipeline drives eight stages. A scale answers four questions and implements none of the stages,
which is the whole of the claim that four scales share one factory rather than four factories
sharing a name.

    find()      where the material comes from   -- an enumerable index, never a model's memory
    specify()   what to build and how to call it
    observe()   which seam: a process, or a call
    probes()    where the inputs come from

Everything else -- building, freezing five times, auditing adequacy, running the evidence battery,
emitting, replaying the emitted package -- is written once and runs identically for all four.

THE TEST OF THIS ABSTRACTION IS MECHANICAL: implementing a new scale must not require editing
anything in `core/`. If it does, either the scale does not belong to this family or something in
`core/` was never really shared. That check is worth more than any amount of agreement about
whether the interface looks clean, so it is a test rather than a paragraph.

A note on what is NOT here. There is no `build()`, no `freeze()`, no `score()`. Those were the
obvious things to put on a scale and every one of them would have been a mistake: they differ
between the two SEAMS, not between the four scales, and a repo task and a package task share a
freeze exactly when they share a seam. Putting them here would have made every scale reimplement
its seam's half of the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Protocol

# The four scales, smallest first. The order is the order they were built in, and it is also the
# order of increasing cost: a kernel is one routine and a repo is a whole program.
SCALES = ("kernel", "module", "package", "repo")


class TaskForm(str, Enum):
    """How the solver is expected to meet the task.

    INPLACE: optimise the existing implementation without changing its language or public API.
    CROSS_LANGUAGE: rewrite the whole surface in a different language to achieve better performance.
    The distinction matters for the instruction (what the solver is asked to do), for the Dockerfile
    (which toolchain is installed), and for the grading framing (behaviour must match; language is
    the lever).
    """

    INPLACE = "inplace"
    CROSS_LANGUAGE = "cross"


@dataclass(frozen=True)
class Candidate:
    """One piece of material, before anyone has decided whether a task can be made from it.

    `identity` is what makes "have we tried this already" answerable. It has to be stable across
    runs -- a URL and a pinned ref, not a position in a search result -- because the alternative is
    a factory that rediscovers the same twenty repositories every night and cannot say how much
    material is left.
    """

    identity: str
    scale: str
    language: str
    source: str                          # which index it came from, for provenance
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scale not in SCALES:
            raise ValueError("unknown scale %r; expected one of %s" % (self.scale, ", ".join(SCALES)))


@dataclass(frozen=True)
class Spec:
    """How to turn one candidate into something runnable and callable.

    Assembled field by field from a validated document, never executed as code on the factory host.
    A model proposes the values; this type is the vocabulary it is allowed to propose in, which is
    what stops it from inventing a mechanism nobody reviewed.
    """

    name: str
    scale: str
    language: str
    description: str
    build: list = field(default_factory=list)          # argv lists, run inside the container
    invoke: list = field(default_factory=list)         # how to start the subject
    entry: str = ""                                    # the symbol under test, on the call seam
    target_language: str = ""
    environment: dict = field(default_factory=dict)
    notes: str = ""
    task_form: TaskForm = TaskForm.INPLACE


class ProbeSource(Protocol):
    """Where a corpus of inputs comes from.

    Two implementations, and they are different mechanisms rather than two settings of one: a schema
    is sampled (a single function's parameters have declarable types), and a generator is executed
    in the container (a package's contract surface has dozens of entry points whose valid inputs
    have nothing in common).
    """

    def draw(self, count: int) -> Iterable: ...


class Observer(Protocol):
    """A seam. Runs the subject on one probe, and compares two observations.

    The pipeline holds one of these and never looks inside an `Observation`. That is what keeps the
    freeze, the comparison and the grading on the far side of the boundary described in `core`'s
    module docstring.
    """

    def run(self, subject, probe): ...

    def compare(self, expected, actual): ...


class Scale(Protocol):
    """The four answers. See the module docstring for why it is these four and not others."""

    name: str

    def find(self, budget: int) -> Iterable[Candidate]: ...

    def specify(self, candidate: Candidate) -> Spec: ...

    def observe(self) -> Observer: ...

    def probes(self, spec: Spec) -> ProbeSource: ...
