"""The boundary that lets four scales share one pipeline.

The design rests on a claim that is easy to state and easy to erode: `core/` does not know what an
observation looks like. Every convenience that breaks it -- reaching into a stdout, special-casing a
returned value -- makes the next scale either edit shared code or pretend its observations have a
shape they do not.

So the claim is a test rather than a promise. It failed once already, on paper: a freeze module was
placed in `core/` because "freeze N times and keep what agrees" sounds universal, when every line
implementing it named argv and stdout and directory trees, which a returned value does not have.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Words that only mean something once you have decided what an observation IS. A module in core/
# that uses one has stopped being neutral about the seam.
_SHAPE_WORDS = re.compile(r"\b(stdout|stderr|exit_code|argv|tree_digest|returned_value)\b")


def _python_files(*parts: str) -> list[str]:
    base = os.path.join(ROOT, *parts)
    found = []
    for dirpath, _, names in os.walk(base):
        found += [os.path.join(dirpath, n) for n in names if n.endswith(".py")]
    return found


def test_core_does_not_know_what_an_observation_looks_like():
    """Anything in core/ naming a channel or a call detail belongs in observe/ instead."""
    offenders = []
    for path in _python_files("frf", "core"):
        for i, line in enumerate(open(path), 1):
            code = line.split("#", 1)[0]          # a comment may discuss the seams; code may not
            if _SHAPE_WORDS.search(code):
                offenders.append("%s:%d %s" % (os.path.relpath(path, ROOT), i, code.strip()[:70]))
    assert not offenders, offenders


def test_core_never_imports_a_seam():
    """Direction of dependency: seams may use core, core may not reach back.

    An import the other way is how a shared layer silently acquires a favourite seam -- and the
    favourite is always the one that existed first, which is not an argument.
    """
    bad = []
    for path in _python_files("frf", "core"):
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if "observe" in name or "scales" in name:
                    bad.append("%s imports %s" % (os.path.relpath(path, ROOT), name))
    assert not bad, bad


def test_a_seam_is_self_contained():
    """Each seam owns its observation type AND its freeze, so neither can drift from the other.

    Splitting them -- the type here, the freeze in a shared module -- is what produced the paper
    error this file exists to catch: a freeze written against one seam's coordinates, applied to
    another seam that has none.
    """
    for seam in ("call",):                       # `process` joins this list when it is written
        base = os.path.join(ROOT, "frf", "observe", seam)
        present = {n for n in os.listdir(base) if n.endswith(".py")}
        assert "observation.py" in present, (seam, present)
        # The freeze lives beside the type it freezes. In this seam that is inside observation.py;
        # what matters is that it is in the seam's own directory, not in core/.
        source = "".join(open(os.path.join(base, n)).read() for n in present)
        assert "def freeze" in source, "%s has no freeze of its own" % seam
