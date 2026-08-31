"""Turning a package into the FUNCTIONS inside it, which is what the module scale sources from.

Every other index in this package lists published artefacts: packages, modules, repositories. The
module scale does not want any of those. It wants one function, with a declared way to call it, and
no registry publishes that -- so this is the adapter, and it is the reason `Module(index=...)` was
otherwise impossible to satisfy from a real registry.

    a registry index      ->   package name and version
    this                  ->   a file, a symbol, and a schema

WHY THIS IS SOURCING AND NOT SPECIFYING. It looks like a job for `specify`, and putting it there was
the first instinct. But `specify` runs on ONE candidate the pipeline has already committed to, and
most functions in a package cannot be served -- they need a class instance, or a file handle, or an
argument whose type nobody declared. Discovering that after committing would make every such package
a refusal attributed to the material, when the truth is that the package was fine and this particular
function was never a candidate. Enumerating callable functions is part of finding material, so it
belongs here, where a rejection costs nothing and is counted honestly.

WHAT IS REFUSED, AND ALL OF IT MECHANICALLY. No model is asked anything; the rule in
`core/sourcing.py` is that an index may be filtered but its members may not be invented, and a
function whose signature cannot be read is not a candidate this index is entitled to guess at.

    third-party imports           the file needs something the offline container will not have
    private names                 a leading underscore is the author saying "not the contract"
    methods                       need an instance, and constructing one is guesswork
    no annotations                the schema would be invented rather than read
    *args / **kwargs              the arity is not fixed, so a probe cannot be drawn
    unsupported types             a parameter this factory has no `kind` for
    generators                    the value is produced lazily, so what came back is not the answer

The last two are the interesting ones. A function returning a generator serialises as an opaque
object and every probe would compare equal to every other, which is a corpus that grades nothing --
and it looks exactly like a working task until somebody reads the expectations.
"""
from __future__ import annotations

import ast
import io
import os
import tarfile
import warnings
import zipfile
from dataclasses import dataclass

from ..core.scale import Candidate
from .http import Http, SourceError

MAX_SCAN_FILES = 500
MAX_SOURCE_FILE_BYTES = 2_000_000

# Python's annotation vocabulary -> the schema kinds in `observe/probes/schema.py`. A table because
# the mapping is data: a type this factory cannot draw is a function it cannot serve, and the honest
# response is to skip the function rather than to draw something else and call it that type.
#
# `list[int]` and friends are read from the subscript, so only the base names appear here.
ANNOTATIONS = {
    "int": {"kind": "int", "low": -1000, "high": 1000},
    "float": {"kind": "float"},
    "bool": {"kind": "bool"},
    "str": {"kind": "string", "size": "n"},
    "bytes": {"kind": "bytes", "size": "n"},
}

# Element types for a declared container. Kept separate from ANNOTATIONS because a bare `list` with
# no element type is NOT drawable -- the elements would be invented -- and merging the two tables
# would make that silently succeed.
ELEMENTS = {
    "int": {"kind": "int", "low": -1000, "high": 1000},
    "float": {"kind": "float", "low": -1000, "high": 1000},
    "bool": {"kind": "bool"},
    "str": {"kind": "string", "size": "n"},
}

# Return annotations that make a function unserveable however well its arguments are declared.
# A generator is the dangerous one: it serialises as an opaque object, so every probe compares equal
# to every other and the corpus grades nothing while looking entirely healthy.
UNSERVEABLE_RETURNS = ("Iterator", "Iterable", "Generator", "AsyncIterator", "AsyncGenerator")

# How many functions one package may contribute. A package with four hundred callable functions
# would otherwise fill a whole batch by itself, and a batch drawn from one package measures that
# package rather than the supply.
PER_PACKAGE = 8

PYPI_FILES = "https://pypi.org/pypi/%s/%s/json"

# Modules a served subject may rely on: the standard library, plus the package's own. Anything else
# is absent from the offline workspace the shim serves from, so the subject dies on import and every
# probe is lost.
#
# MEASURED, NOT SUPPOSED. On a twenty-candidate run this was eight of the ten refusals -- networkx,
# psutil, torch -- and each one cost a download, an unpack, a build and five freeze passes before
# failing. It is decidable from the file's own import statements, so it is decided here, where a
# rejection costs nothing.
_STDLIB = frozenset(getattr(__import__("sys"), "stdlib_module_names", ()))


@dataclass
class Function:
    """One callable function, located in a file, with a way to call it."""

    package: str
    version: str
    module: str
    symbol: str
    path: str
    schema: dict
    doc: str

    @property
    def identity(self) -> str:
        return "pypi:%s@%s#%s.%s" % (self.package, self.version, self.module, self.symbol)


class PythonFunctions:
    """Functions inside packages, sourced from whatever package index it is given.

    Composed with a registry index rather than replacing one: the packages still come from an
    enumerable source that can be paged and counted, and this widens each of them into the functions
    it contains. `total()` is therefore the package total -- honest, and the denominator a yield
    should be computed against, since the unit of supply really is the package.
    """

    name = "python-functions"

    def __init__(self, packages, http: Http | None = None, *, workspace: str = "",
                 per_package: int = PER_PACKAGE, scale: str = "module") -> None:
        self._packages = packages
        self._http = http or Http()
        self._workspace = workspace or os.path.join("work", "sources")
        self._per_package = per_package
        self._scale = scale

    def total(self) -> int | None:
        """The package total, not a function total.

        There is no way to know how many functions exist without downloading every package, and
        inventing a multiplier would be exactly the fabricated denominator `core/sourcing.py`
        forbids. The package count is real and is the honest thing to divide by.
        """
        return self._packages.total()

    def page(self, number: int, *, size: int = 20):
        """One page of packages, widened into the functions inside them.

        A package that cannot be downloaded or holds nothing callable contributes NOTHING and is not
        an error: most packages are like that, and it is the ordinary shape of this supply rather
        than a failure. What would be an error is a page that came back empty because the registry
        misbehaved, and that is `self._packages`' business -- it raises, and the raise travels.
        """
        found = []
        spent = getattr(self, "already_seen", None) or ()
        for candidate in self._packages.page(number, size=size):
            # A REPOSITORY ALREADY DRAWN FROM IS NOT WORTH MINING AGAIN, and the saving is this
            # page's whole cost: `materialise` downloads it and `scan` parses every file, and the
            # dedup in `sourcing.walk` only runs afterwards. A restarted roll with a seeded seen-set
            # spent nine minutes re-mining repositories it had already produced from, and made one
            # attempt. `max_per_repository` already caps how many tasks one repository contributes,
            # so returning to one is waste even when it has functions left.
            if spent and _already_drawn_from(candidate.identity, spent):
                continue
            detail = candidate.detail or {}
            name = str(detail.get("package") or "")
            version = str(detail.get("version") or "")
            if not name or not version:
                continue
            try:
                root = self.materialise(name, version)
            except SourceError:
                # One package that will not download is one package lost. Raising here would end a
                # batch because a single sdist was missing.
                continue
            for function in scan(root, name, version)[:self._per_package]:
                found.append(to_candidate(function, scale=self._scale))
        return found

    def materialise(self, package: str, version: str) -> str:
        """Download and unpack one package's source. -> the directory it landed in.

        The SOURCE distribution, not the wheel: a wheel of a compiled package holds a shared object
        with nothing to read, and the module scale needs a file it can serve and perturb. A package
        with no sdist is skipped rather than served from its wheel.
        """
        room = os.path.join(self._workspace, "%s-%s" % (package, version))
        if os.path.isdir(room):
            return room

        payload = self._http.json(PYPI_FILES % (package, version))
        urls = [f for f in (payload.get("urls") or ()) if f.get("packagetype") == "sdist"]
        if not urls:
            raise SourceError("%s %s publishes no source distribution" % (package, version))

        blob = self._http.get(str(urls[0].get("url") or ""))
        os.makedirs(room, exist_ok=True)
        _unpack(blob, str(urls[0].get("filename") or ""), room)
        return room


def _unpack(blob: bytes, filename: str, room: str) -> None:
    """Unpack an sdist, refusing members that would write outside the destination.

    The same reasoning as the sandbox's unpacker: this is an archive from the internet, and a member
    named `../../something` is what an attempt to escape looks like. Only Python sources are kept --
    everything else in an sdist is documentation, tests and packaging metadata that the scan would
    walk over anyway.
    """
    destination = os.path.abspath(room)

    def _safe(name: str) -> bool:
        if not name.endswith(".py"):
            return False
        target = os.path.abspath(os.path.join(destination, name))
        return target.startswith(destination + os.sep)

    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for entry in archive.namelist():
                if _safe(entry):
                    archive.extract(entry, destination)
        return
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
        members = [m for m in archive.getmembers() if m.isfile() and _safe(m.name)]
        archive.extractall(destination, members=members)


def scan(root: str, package: str = "", version: str = "") -> list:
    """Every function in a source tree that this factory could actually serve.

    Read with `ast`, never imported. Importing a package to look at it runs its module-level code --
    which is somebody else's code, executed on the factory's host, to answer a question about its
    shape. The rule that model-written code never runs here would be worth very little beside a
    sourcing step that executes every package on PyPI.
    """
    found = []
    for path in _python_files(root):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(open(path, encoding="utf-8", errors="replace").read(), path)
        except (OSError, SyntaxError, ValueError):
            # A file that will not parse is a file skipped. sdists contain Python 2, templates and
            # deliberately broken fixtures, and none of that is a reason to lose the package.
            continue
        module = _module_name(root, path)
        foreign = _foreign_imports(tree, package)
        if foreign:
            # One import the container will not have loses every function in the file, so this is
            # checked once per file rather than once per function.
            continue
        for node in tree.body:                      # top level only: see the module docstring
            if not isinstance(node, ast.FunctionDef):
                continue
            schema = schema_for(node)
            if schema is None or not _work_scales_with_input(node, schema):
                continue
            found.append(Function(package=package, version=version, module=module,
                                  symbol=node.name, path=path, schema=schema,
                                  doc=(ast.get_docstring(node) or "").strip()))
    found.sort(key=lambda f: (f.module, f.symbol))
    return found


_SIZED_KINDS = frozenset({"int_array", "float_array", "complex_array", "string", "bytes", "list", "map"})

def _work_scales_with_input(node: ast.FunctionDef, schema: dict) -> bool:
    """Static workload signal; deliberately not a headroom measurement."""
    if {p.get("kind") for p in schema.get("params", ())} & _SIZED_KINDS:
        return True
    return any(isinstance(inner, (ast.For, ast.While, ast.ListComp, ast.SetComp,
                                  ast.DictComp, ast.GeneratorExp))
               for inner in ast.walk(node))


def _foreign_imports(tree: ast.Module, package: str) -> list:
    """Which modules this file needs that a served subject will not have.

    Relative imports (`from . import x`) count as foreign too: they resolve inside the distribution
    and not beside a shim, so a file using one cannot be served standing alone. That is the same
    failure as a missing wheel and is reported the same way.
    """
    own = {package.replace("-", "_").lower(), package.lower()}
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:                          # `from . import x`, relative to the package
                missing.append("." * node.level + (node.module or ""))
                continue
            names = [node.module or ""]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            if root and root not in _STDLIB and root.lower() not in own:
                missing.append(name)
    return missing


def schema_for(node: ast.FunctionDef) -> dict | None:
    """A function definition -> a schema that can draw its arguments, or None if it cannot.

    None is the common answer and is not a failure. Most functions in a real package take a class
    instance, an open file, or an argument nobody annotated, and the honest thing is to leave them
    alone -- a guessed schema draws the wrong shape, and the freeze afterwards records it happily.
    """
    if node.name.startswith("_"):
        return None
    if any(_decorator_name(d) in ("property", "staticmethod", "classmethod") for d in
           node.decorator_list):
        return None

    arguments = node.args
    # No fixed arity, so there is no argument list to draw. A subject called with the wrong number
    # of arguments refuses every probe, which freezes as a corpus of identical refusals.
    if arguments.vararg is not None or arguments.kwarg is not None:
        return None
    positional = list(arguments.posonlyargs) + list(arguments.args)
    if not positional or len(positional) > 4:
        # Nothing to vary, or so many parameters that the draw is unlikely to satisfy whatever
        # relationship the function requires between them.
        return None
    if positional[0].arg in ("self", "cls"):
        return None
    if _returns_unserveable(node):
        return None

    params = []
    for argument in positional:
        param = _param_for(argument.annotation)
        if param is None:
            return None
        params.append(param)
    return {"params": params}


def _param_for(annotation) -> dict | None:
    """One annotation -> one schema parameter, or None when it cannot be drawn.

    Deliberately narrow. Every type this refuses is a function skipped, which costs a little supply;
    every type it accepts WRONGLY is a corpus drawn in the wrong shape, which costs a task that looks
    healthy and grades nothing. The asymmetry decides the design.
    """
    if annotation is None:
        return None
    name = _annotation_name(annotation)
    if name in ANNOTATIONS:
        return dict(ANNOTATIONS[name])

    # A container with a declared element type: `list[int]`, `Sequence[float]`. The element is what
    # makes it drawable -- a bare `list` is a list of what?
    if isinstance(annotation, ast.Subscript) and name in ("list", "List", "Sequence", "Iterable"):
        inner = _annotation_name(annotation.slice)
        if inner in ("int", "float"):
            # Numeric sequences become arrays so that a size can be named and varied, which is what
            # lets one schema be drawn at several shapes for the timing pass.
            return {"kind": "%s_array" % ("int" if inner == "int" else "float"),
                    "dtype": "int64" if inner == "int" else "float64", "size": "n"}
        if inner in ELEMENTS:
            return {"kind": "list", "size": "n", "element": dict(ELEMENTS[inner])}
    return None


def _returns_unserveable(node: ast.FunctionDef) -> bool:
    """Whether what comes back cannot be compared across the wire.

    A generator is the case worth naming: it crosses as an opaque object, so every probe's answer
    equals every other probe's, and the corpus grades nothing while looking entirely healthy. That
    is the failure this whole module is most likely to produce, so it is refused explicitly.
    """
    if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)):
        return True
    returns = getattr(node, "returns", None)
    if returns is None:
        return False
    return _annotation_name(returns) in UNSERVEABLE_RETURNS


def _annotation_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value                          # a string annotation, from `from __future__`
    return ""


def _decorator_name(node) -> str:
    if isinstance(node, ast.Call):
        return _annotation_name(node.func)
    return _annotation_name(node)


def _module_name(root: str, path: str) -> str:
    relative = os.path.relpath(path, root)
    return relative[:-3].replace(os.sep, ".").replace(".__init__", "")


def _python_files(root: str):
    checked = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in ("test", "tests", "docs", "examples", "__pycache__", ".git")]
        for name in files:
            if name.endswith(".py") and not name.startswith("test_"):
                path = os.path.join(directory, name)
                try:
                    if os.path.getsize(path) > MAX_SOURCE_FILE_BYTES:
                        continue
                except OSError:
                    continue
                if checked >= MAX_SCAN_FILES:
                    return
                checked += 1
                yield path


def to_candidate(function: Function, *, scale: str = "module",
                 source: str = "python-functions") -> Candidate:
    """One located function -> a Candidate the module scale can specify.

    This is the whole point of the module: `detail` carries exactly what `Module._locate` requires,
    so a registry index and a scale that never heard of each other are joined without either
    learning about the other.
    """
    return Candidate(
        identity=function.identity,
        scale=scale, language="python", source=source,
        detail={
            "source_path": function.path,
            "symbol": function.symbol,
            "schema": function.schema,
            "description": function.doc.splitlines()[0][:200] if function.doc else
                           "the %s function from %s" % (function.symbol, function.package),
            # The package it came from is what a submission must not reach for. Named here so the
            # delegation check can look for it without the scale knowing about registries.
            "forbidden": [function.package],
            "package": function.package, "version": function.version,
            "module": function.module,
        })


def _already_drawn_from(identity: str, spent) -> bool:
    """Whether anything already walked came out of this repository. -> True to skip it.

    A function identity is its repository's plus a `#path.symbol` suffix, so the repository is a
    prefix of everything mined from it. Checked as a prefix rather than by parsing, because the
    suffix separator differs between the scales that use this.
    """
    if identity in spent:
        return True
    return any(seen.startswith(identity + "#") for seen in spent)
