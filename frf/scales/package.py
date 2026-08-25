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
import shutil
from dataclasses import dataclass, field

from ..core import integrity
from ..core.contract import PackageContract, PackageOperation, Provenance
from ..core.scale import Candidate, Spec
from ..observe import coverage
from ..observe.call import shims
from ..observe.call.runner import Subject, RemoteSubject
from .module import PROBE_TIMEOUT

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
    dispatch: tuple = ()
    package_name: str = ""
    package_root: str = ""
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
        self._isolated = False

    def build(self, spec: Spec) -> None:
        """Copy the pinned package, create a dispatch adapter, and serve it over JSON."""
        if not self.material.package_root or not self.material.package_name:
            raise RuntimeError("package material has no package root/name")
        os.makedirs(self.workspace, exist_ok=True)
        destination = os.path.join(self.workspace, self.material.package_name)
        shutil.copytree(self.material.package_root, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".git"))
        dispatch = {entry["name"]: (entry["module"], entry["symbol"])
                    for entry in self.material.dispatch}
        native = spec.language in ("javascript", "typescript")
        adapter = os.path.join(self.workspace, "subject.js" if native else "subject.py")
        with open(adapter, "w", encoding="utf-8") as handle:
            handle.write(_native_adapter_source(dispatch) if native else _adapter_source(dispatch))
        _, self._argv = shims.materialise(self.workspace, spec.language, adapter, "entry")

    def subject(self, spec: Spec | None = None, *, mutated: bool = False,
                attempt: int = 0):
        argv, room = self._argv, self.workspace
        if mutated:
            room = os.path.join(self.workspace, ".mutant-%d" % attempt)
            shutil.rmtree(room, ignore_errors=True)
            shutil.copytree(self.workspace, room, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".mutant-*", "__pycache__"))
            adapter = os.path.join(room, "subject.py")
            original = open(adapter, encoding="utf-8").read()
            if attempt == 0:
                original += "\n_old_entry = entry\ndef entry(op, *args):\n    return None\n"
            elif attempt == 1:
                original += "\n_old_entry = entry\ndef entry(op, *args):\n    return 0\n"
            with open(adapter, "w", encoding="utf-8") as handle:
                handle.write(original)
            _, argv = shims.materialise(room, spec.language, adapter, "entry")
        if getattr(self._backend, "name", "") in ("docker", "remote"):
            return RemoteSubject(argv, workspace=room, backend=self._backend,
                                 timeout=PROBE_TIMEOUT)
        return Subject(argv, cwd=room, timeout=PROBE_TIMEOUT)

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
        allowed = tuple({self.material.package_name, *
                         (entry.get("module", "") for entry in self.material.dispatch)})
        return [str(hit) for hit in integrity.inspect(
            self.workspace, banned, allowed=allowed).hits]

    def isolation(self):
        """How the two sides are kept apart while one is timed -- reported, never assumed."""
        return integrity.isolation_for(self._backend, applied=self._isolated)

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
        # The observer, once built. See observe().
        self._built = None

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

        page_size = 4 if getattr(self._index, "name", "") == "github-packages" else 20
        return sourcing.walk(self._index, budget, page_size=page_size)

    def specify(self, candidate: Candidate) -> Spec:
        self._material = self._locate(candidate)
        # A new candidate means a new subject, so the cached observer is stale. Not
        # resetting it would serve the previous candidate for the rest of a batch --
        # every task after the first describing material it was not built from.
        self._built = None
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
        # Cached for the same reason as the module scale: build() and freeze() must reach the
        # same observer, or the one that is frozen is the one that was never built.
        if self._built is None:
            self._built = Observer(self._workspace, self._material, backend=self._backend)
        return self._built

    def probes(self, spec: Spec) -> ProbeSource:
        """Run the generator in the sandbox, then mechanically audit its data output."""
        if self._material is None:
            raise RuntimeError("probes() was asked for before specify() chose a package")
        if not self._material.generator:
            raise ValueError("package %s has no probe generator; a surface cannot be sampled from a "
                             "schema, so one is required" % self._material.identity)
        if self._run_generator is None:
            raise RuntimeError(
                "no runner was given for the probe generator. Generators are model-written and must "
                "execute inside a container; Package(run_generator=...) is how that is supplied.")
        try:
            print("[package] running probe generator for %s" % self._material.identity, flush=True)
            drawn = self._run_generator(self._material.generator, PROBE_COUNT)
            print("[package] probe generator completed for %s" % self._material.identity, flush=True)
        except Exception as exc:
            # One repair is cheap compared with discarding a package after a model formatting or
            # dispatch-shape mistake. The repaired code still executes only in E2B and is audited
            # by the same contract below.
            try:
                from ..core import model
                from ..core.model import validated_generator
                repair = model.ask(
                    "Repair this probes(n) generator. It must return a dict with probes and labels; "
                    "each probe is [operation_name, ...], labels are valid/error/boundary, all "
                    "values JSON-safe, every dispatch name covered twice, and return at least 60 "
                    "distinct probes. Return code only.\n"
                    "Error from sandbox: %s\nGenerator:\n%s" %
                    (str(exc)[:1800], self._material.generator),
                    system="Return only valid Python defining probes(n).", timeout=60)
                repaired = validated_generator(repair)
                print("[package] retrying repaired probe generator for %s" % self._material.identity,
                      flush=True)
                drawn = self._run_generator(repaired, PROBE_COUNT)
            except Exception as repair_exc:
                raise ValueError("package probe generator failed in the sandbox: %s; repair failed: %s"
                                 % (str(exc)[:900], str(repair_exc)[:900])) from repair_exc
        labels = None
        if isinstance(drawn, dict):
            labels = drawn.get("labels")
            drawn = drawn.get("probes")
        probes = _as_argument_lists(drawn)
        _audit_probe_contract(probes, self._material.dispatch, labels=labels)
        from dataclasses import replace
        counts = {}
        for probe in probes:
            counts[str(probe[0])] = counts.get(str(probe[0]), 0) + 1
        classes = {}
        for label in labels or []:
            classes[label] = classes.get(label, 0) + 1
        self._spec = replace(spec, notes=(spec.notes or "") +
                             " package coverage: %d operation(s), %d probe(s), operations=%s, classes=%s" %
                             (len(counts), len(probes), json.dumps(counts, sort_keys=True),
                              json.dumps(classes, sort_keys=True)))
        return ProbeSource(probes)

    def _locate(self, candidate: Candidate) -> Material:
        detail = candidate.detail or {}
        if "entry_points" not in detail:
            raise ValueError("candidate %s does not name its public surface; a package task grades "
                             "the whole contract, so the entry points are required"
                             % candidate.identity)
        from ..source.package_adapters import operations
        raw_ops = detail.get("dispatch") or operations(
            str(detail.get("root") or ""), candidate.language,
            str(detail.get("package_name") or ""), str(detail.get("package_root") or ""))
        dispatch = tuple(raw_ops)
        if not dispatch:
            raise ValueError("candidate %s has no supported public package operations" % candidate.identity)
        generator = detail.get("generator", "")
        if not generator:
            from ..core import model
            from ..core.model import validated_generator
            surface = json.dumps(dispatch, sort_keys=True, indent=2)
            print("[package] requesting probe generator for %s" % candidate.identity, flush=True)
            answer = model.ask(
                "Write deterministic probes(n) returning a dict with probes (argument lists) and "
                "labels (one of valid,error,boundary for each probe) for this public dispatch. "
                "Each probe must be [operation_name, arg1, arg2, ...], with operation_name exactly "
                "one of the dispatch names; the operation name is not omitted. "
                "The dispatch below is a JSON list of objects with keys name/module/symbol/signature; "
                "iterate those objects by entry['name'], never unpack entries as (name, cases). "
                "Example output shape: {'probes': [['op', 'x']], 'labels': ['valid']}. "
                "Return at least 60 distinct probes even when n is smaller. "
                "Every argument must be JSON-serializable (null, boolean, number, string, list, "
                "or object with string keys); never return sets, tuples, bytes, objects, or callables. "
                "Include valid, invalid and boundary cases for every operation. "
                "Return only code.\n" + surface,
                system="Define only a top-level probes(n) generator. Do not execute the package.",
                timeout=60)
            try:
                generator = validated_generator(answer)
                print("[package] received probe generator for %s" % candidate.identity, flush=True)
            except Exception:
                answer = model.ask("Return ONLY valid Python defining probes(n).\n" + surface,
                                   system="Define exactly probes(n).", timeout=60)
                generator = validated_generator(answer)
        operations = tuple(PackageOperation(str(entry.get("name") or ""),
                                             str(entry.get("module") or ""),
                                             str(entry.get("symbol") or ""),
                                             str(entry.get("signature") or ""),
                                             bool(entry.get("json_safe", True)))
                           for entry in dispatch)
        contract = PackageContract(candidate.identity, str(detail.get("package_name") or ""),
                                   operations,
                                   provenance=Provenance(candidate.identity,
                                                         "static-package-survey",
                                                         evidence=(str(detail.get("package_root") or ""),)))
        contract.validate()
        return Material(candidate.identity, candidate.language, detail.get("root", ""),
                        tuple(entry.get("name") for entry in dispatch),
                        detail.get("description", ""), dispatch,
                        str(detail.get("package_name", "")),
                        str(detail.get("package_root", "")), generator,
                        detail.get("target_language", ""),
                        tuple(detail.get("forbidden", ())),
                        list(detail.get("install", ())))


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

def _audit_probe_contract(probes: list, dispatch: tuple, *, labels=None) -> None:
    """Reject generator output that cannot cover a package contract honestly."""
    if len(probes) < 60:
        raise ValueError("package generator returned only %d probes; need at least 60" % len(probes))
    names = {str(entry.get("name")) for entry in dispatch if entry.get("name")}
    seen = set()
    counts = {name: 0 for name in names}
    for index, args in enumerate(probes):
        if not args or not isinstance(args[0], str):
            raise ValueError("package probe %d does not start with an operation name" % index)
        operation = args[0]
        if operation not in names:
            raise ValueError("package probe %d names unknown operation %r" % (index, operation))
        seen.add(json.dumps(args, sort_keys=True, separators=(",", ":"), default=str))
        counts[operation] += 1
    if len(seen) * 2 < len(probes):
        raise ValueError("package generator produced too many duplicate probes")
    # One probe only proves that an operation can be named. Two distinct probes are the
    # minimum evidence that its behavior, rather than just its dispatch wrapper, is being
    # graded. Larger valid/error/boundary balance remains the generator's responsibility.
    missing = [name for name, count in counts.items() if count < 2]
    if missing:
        raise ValueError("package generator did not cover operations with at least two probes: %s"
                         % ", ".join(sorted(missing)))
    if labels is not None:
        if not isinstance(labels, list) or len(labels) != len(probes):
            raise ValueError("package probe labels must align one-for-one with probes")
        allowed = {"valid", "error", "boundary"}
        unknown = set(labels) - allowed
        if unknown:
            raise ValueError("package probe labels contain unknown classes: %s" %
                             ", ".join(sorted(unknown)))
        classes = set(labels)
        if classes != allowed:
            raise ValueError("package generator must include valid, error and boundary probes")



def _task_name(material: Material) -> str:
    stem = material.identity.rsplit("/", 1)[-1].replace("_", "-").lower()
    return "%s-rewrite" % stem if material.target_language else "%s-faster" % stem


def _adapter_source(dispatch: dict) -> str:
    return """import importlib

_DISPATCH = %r


def entry(op, *args):
    if op not in _DISPATCH:
        raise ValueError("unknown operation: %%s" %% op)
    module_name, symbol = _DISPATCH[op]
    return getattr(importlib.import_module(module_name), symbol)(*args)
""" % dispatch


def _native_adapter_source(dispatch: dict) -> str:
    return """const DISPATCH = %r;
exports.entry = async function(op, ...args) {
  if (!DISPATCH[op]) throw new Error('unknown operation: ' + op);
  const [mod, symbol] = DISPATCH[op];
  let loaded;
  try { loaded = await import(mod); } catch (e) { loaded = require(mod); }
  const fn = loaded[symbol] || (loaded.default && loaded.default[symbol]) || loaded.default;
  if (typeof fn !== 'function') throw new Error('export is not callable: ' + symbol);
  return fn(...args);
};
""" % dispatch
