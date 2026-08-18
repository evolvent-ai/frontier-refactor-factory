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
_SHAPE_WORDS = re.compile(r"\b(stdout|stderr|exit_code|tree_digest|returned_value)\b"
                          # `argv` only counts as an OBSERVATION's field. `sys.argv` is a program
                          # reading its own arguments, which every language has and which says
                          # nothing about the seam -- flagging it made this check cry wolf on the
                          # first module that shipped an entry point.
                          r"|(?<!sys\.)\bargv\b")

# THE WORDS ARE AMBIGUOUS AND THE PROPERTY IS NOT. A subprocess result has a stdout; so does a
# process-seam observation; they are not the same thing, and no regex can tell them apart. What
# distinguishes them is what happens to the value: an OBSERVATION is something the pipeline freezes
# and grades, and a command's output is something it reads once and discards.
#
# So modules that only ever run commands are exempt from the word check and are held to the
# stronger property instead -- they must not freeze or grade anything. Listing them here is
# deliberate friction: adding a name to this list is a claim, and the test below checks it.
_RUNS_COMMANDS = {"sandbox.py", "containers.py", "integrity.py"}


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
        if os.path.basename(path) in _RUNS_COMMANDS:
            continue
        for i, line in enumerate(open(path), 1):
            code = line.split("#", 1)[0]          # a comment may discuss the seams; code may not
            if _SHAPE_WORDS.search(code):
                offenders.append("%s:%d %s" % (os.path.relpath(path, ROOT), i, code.strip()[:70]))
    assert not offenders, offenders


def test_a_module_exempted_from_the_word_check_really_only_runs_commands():
    """The exemption is a claim, so it is checked rather than trusted.

    A module allowed to say `stdout` because it runs commands must not be the module that decides
    what is correct. If it ever freezes or grades, the word it was excused for has become an
    observation after all, and the exemption is hiding exactly what it was meant to permit.
    """
    for name in _RUNS_COMMANDS:
        path = os.path.join(ROOT, "frf", "core", name)
        assert os.path.exists(path), "%s is exempted but does not exist" % name
        source = open(path).read()
        tree = ast.parse(source)
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not {"freeze", "grade"} & defined, (
            "%s runs commands AND %s -- then its streams are observations and it belongs in a seam"
            % (name, sorted({"freeze", "grade"} & defined)))


def test_a_seam_never_imports_a_scale():
    """Direction of dependency, one layer down. A seam is what a scale is written against.

    Added after a seam imported a constant from `scales/module.py` -- which worked, and which no
    test caught, because the check below only guards `core/`. A seam that reaches into a scale has
    inverted the dependency the layout exists to keep, and the next scale to want that constant
    would have to import from a sibling or copy it.
    """
    bad = []
    for path in _python_files("frf", "observe"):
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                # A relative import records its module as None or a bare name, so the dots are
                # what say how far up it reached. `from ...scales.x import y` is level 3.
                if node.level and node.module:
                    names = ["%s%s" % ("." * node.level, node.module)]
            for name in names:
                if "scales" in name:
                    bad.append("%s imports %s" % (os.path.relpath(path, ROOT), name))
    assert not bad, bad


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
    for seam in ("call", "process"):
        base = os.path.join(ROOT, "frf", "observe", seam)
        present = {n for n in os.listdir(base) if n.endswith(".py")}
        assert "observation.py" in present, (seam, present)
        # The freeze lives beside the type it freezes. In this seam that is inside observation.py;
        # what matters is that it is in the seam's own directory, not in core/.
        source = "".join(open(os.path.join(base, n)).read() for n in present)
        assert "def freeze" in source, "%s has no freeze of its own" % seam
