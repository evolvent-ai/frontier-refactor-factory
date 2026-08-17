"""frontier-refactor-factory -- performance-oriented refactoring tasks, at four scales.

The factory takes real code and produces a task that asks for it to be made faster, or reimplemented
in another language, without changing what it does. It decides whether a submission did that by
running it, never by reading it, and it establishes the correct behaviour by MEASURING the reference
rather than by anyone writing down what the answer should be.

    from frf import Factory

    factory = Factory()                                   # every scale, sandboxed, defaults
    result = factory.build("module", budget=20)
    print(result.summary())

Four scales, smallest first:

    kernel    one computational routine
    module    one function or symbol
    package   a package's whole public surface
    repo      an entire repository

They differ in two places and agree everywhere else. The two are WHERE THE MATERIAL COMES FROM and
WHERE THE SUBJECT IS OBSERVED -- a repository is watched as a process (exit code, stdout, stderr, the
files it leaves behind), while the three smaller scales are called over a JSON wire. Everything after
that -- freezing five runs into an expectation, auditing whether the corpus reaches what the program
actually does, running the evidence battery, emitting, and replaying what was emitted -- is one
implementation shared by all four.

What comes out is a Harbor task package: a workspace with the reference in it, a statement, and a
verifier that scores correctness first and speed only afterwards.
"""
from __future__ import annotations

from .core.scale import SCALES, Candidate, Scale, Spec
from .factory import Factory, Settings

__all__ = ["Factory", "Settings", "Candidate", "Scale", "Spec", "SCALES", "__version__"]

__version__ = "0.1.0"
