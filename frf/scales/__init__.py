"""The four scales. Each answers four questions and implements none of the eight stages.

    kernel    one computational routine        -- a module with arrays, an envelope, a pluggable cost
    module    one function or symbol           -- the smallest instance of the call seam
    package   a package's whole public surface -- the call seam, with generated probes
    repo      an entire repository             -- the process seam

Three of the four share one seam and the fourth uses the other, and that split is the design's only
structural divide. Everything downstream of an observation is shared, which is why these files are
short: `kernel.py` is eighty lines because a kernel really is a module with three additions, and
saying that in code is better than saying it in a comment above a copy.
"""
from __future__ import annotations

from .kernel import Kernel
from .module import Module
from .package import Package
from .repo import Repo

__all__ = ["Kernel", "Module", "Package", "Repo"]
