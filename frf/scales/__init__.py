"""The four scales. Each answers four questions and implements none of the eight stages.

    kernel    one computational routine        -- a process-seam repo subclass
    module    one function or symbol           -- a process-seam repo subclass
    package   a library's whole public surface -- a process-seam repo subclass
    repo      an entire repository             -- the process seam

All four scales use the process seam and source from GitHub. The differences are in labelling,
expected candidate profile, and task metadata — not in how the pipeline observes them.
"""
from __future__ import annotations

from .kernel import Kernel
from .module import Module
from .package import Package
from .repo import Repo

__all__ = ["Kernel", "Module", "Package", "Repo"]
