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
import uuid
import shutil
from dataclasses import dataclass, field

from ..core import integrity
from ..core.contract import PackageContract, PackageOperation, Provenance
from ..core.scale import Candidate, Spec, TaskForm
from ..observe import coverage
from ..observe.call import dispatch as call_dispatch
from ..observe.call import shims
from ..observe.call.runner import Subject, RemoteSubject
from .module import BUILD_TIMEOUT, PROBE_TIMEOUT

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
        # The surface the dispatcher fans out to, kept because a mutant is
        # REGENERATED from it rather than patched onto the real subject's source.
        self._dispatch: dict = {}

    def build(self, spec: Spec) -> None:
        """Copy the pinned package, create a dispatch adapter, and serve it over JSON."""
        if not self.material.package_root or not self.material.package_name:
            raise RuntimeError("package material has no package root/name")
        os.makedirs(self.workspace, exist_ok=True)
        destination = os.path.join(self.workspace, self.material.package_name)
        shutil.copytree(self.material.package_root, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".git"))
        # THE DISPATCHER AND THE MANIFEST MUST BE IN THE SAME DIRECTORY, and this once was not: the
        # repo went to `workspace/<package_name>/` (where go.mod lives), while the dispatcher and
        # shim were written at the WORKSPACE ROOT. A Go build `go build serve.go subject.go` run from
        # that root has no go.mod in scope, so every static-language candidate failed to build there
        # -- and because the failure text carries a fresh `/tmp/frf-subject-<uuid>` per run, freeze
        # read the five different messages as five different answers and discarded 100% of probes,
        # charged to the material. That is the whole of the freeze-100% mystery: a directory mismatch
        # behind a random path.
        dispatch = {entry["name"]: ((entry["module"], entry["symbol"], entry.get("klass") or "",
                                     entry.get("params") or (), entry.get("result") or {}))
                    for entry in self.material.dispatch}
        self._dispatch = dispatch
        # GO NEEDS AN INDEPENDENT PACKAGE, and the dispatcher/shim live in a subpackage of the module
        # rather than beside its sources. The module's own packages are `package cayley`, `package
        # graph`, and a `page main` dispatcher at the repo root collides with them; a `frfd/`
        # subpackage is a peer that imports them (`graph.IsPersistent`), which `go build .` inside it
        # resolves through go.mod -- the same module scope the earlier layout fix established, but
        # without package-name collisions. Other languages keep the dispatcher beside their sources.
        if spec.language == "go":
            destination = os.path.join(destination, "frfd")
            os.makedirs(destination, exist_ok=True)
        adapter = os.path.join(destination, _subject_name(spec.language))
        with open(adapter, "w", encoding="utf-8") as handle:
            handle.write(_dispatcher_source(spec.language, dispatch))
        build, self._argv = shims.materialise(destination, spec.language, adapter, "entry",
                                              subject_name=_subject_name(spec.language),
                                              served_name=_served_name(spec.language))

        # A COMPILED LANGUAGE HAS TO BE COMPILED, and this scale never did it. `Observer.build` wrote
        # the files and stopped; `RemoteSubject` pushes a tree and executes argv. For Go that argv is
        # `serve.bin` -- a binary nothing had produced. Every probe therefore failed to start, and
        # because the failure text carries a per-run `/tmp/frf-subject-<uuid>`, freeze saw five
        # different answers and discarded 100% of the corpus as unstable MATERIAL. Eight of eight Go
        # candidates in every batch, for a build step that did not exist. The module scale has always
        # compiled here; this is that same step, which the package scale was missing.
        # A PACKAGE'S DEPENDENCIES ARE PART OF WHAT IT IS, and nothing here installed them. The
        # material arrives from `github_package._materialise`, which runs `git init`, `fetch --depth 1`
        # and `checkout` -- and stops. So a package that imports anything beyond its standard library
        # could never be served: the shim loaded it, the import failed, and every probe came back
        # `ok=False` with the same message.
        #
        # THAT IS WHY THIS SCALE IS PYTHON AND GO. Of 53 attested package tasks, 36 are python, 9 go,
        # 8 javascript -- and every one of them has a pure-stdlib dependency closure. It read as a
        # sourcing bias and it was not: 102 attempts across javascript, typescript, ruby and rust
        # emitted NOTHING, and the javascript and typescript failures were all one error.
        # `observation.digest` normalises `Cannot find module` and `ERR_MODULE_NOT_FOUND` to
        # `Error: module resolution failed`, which is the exact text `package/algebra-latex-faster`
        # shipped 57 frozen probes of.
        #
        # INSIDE THE SANDBOX, for the same reason the compile below is: an install on the factory host
        # resolves against the host's toolchain and registry, and the expectation would then describe
        # a package the delivered image does not contain. The sandbox has a network -- it clones from
        # GitHub here too, which is what `shims/__init__` records about `GOPROXY=off` being wrong.
        install = _install_commands(spec.language)
        if not build and not install:
            return
        if self._backend is None or getattr(self._backend, "name", "") == "local-process":
            raise RuntimeError(
                "%s needs a sandbox to prepare its subject in; refusing to build or install on the "
                "host, whose toolchain is not what the task ships with" % spec.language)
        remote = "/tmp/frf-package-build-%s" % uuid.uuid4().hex[:12]
        self._backend.push(self.workspace, remote)
        # THE BUILD RUNS INSIDE THE MODULE, not at the pushed root. Go resolves `go.mod` by walking
        # UP from the working directory, and the manifest sits in `<package_name>/` -- one level below
        # the tree that was pushed. Building from the root reported `go.mod file not found in current
        # directory or any parent directory` for every imported package.
        module_root = remote + "/" + self.material.package_name
        # DEPENDENCIES BEFORE THE COMPILE, because a TypeScript compile resolves imports through
        # `node_modules` and would otherwise report every one of them as missing.
        #
        # BEST EFFORT, DELIBERATELY. `npm install` fetches devDependencies too, so one broken
        # development tool would refuse a package whose runtime dependencies are perfectly fine.
        # A package that genuinely cannot import itself is caught a stage later, by the freeze gate
        # that asks whether the subject ever answered -- and that gate reports the subject's own
        # error, which is a better diagnosis than an installer's exit code. What is NOT acceptable is
        # the old behaviour: not trying at all, and calling the resulting import failure material.
        for command in install:
            done = self._backend.run(command, workdir=module_root, timeout=BUILD_TIMEOUT)
            if not done.ok:
                print("[package] %s: %s did not complete: %s"
                      % (self.material.identity, " ".join(command[:2]), done.tail(300)), flush=True)
        for command in build:
            remote_command = [part.replace(self.workspace, remote) if isinstance(part, str) else part
                              for part in command]
            done = self._backend.run(remote_command, workdir=module_root, timeout=BUILD_TIMEOUT)
            if not done.ok:
                raise RuntimeError("%s did not build: %s" % (self.material.identity, done.tail(800)))
        # Bring the built artefact AND THE INSTALLED DEPENDENCIES back, so the workspace the freeze
        # pushes carries them. `RemoteSubject.__enter__` already keeps `node_modules` when it pushes
        # -- its comment says the dependency tree "is part of the contract" -- but until now there was
        # never anything there to keep.
        self._backend.pull(remote, self.workspace)

    def subject(self, spec: Spec | None = None, *, mutated: bool = False,
                attempt: int = 0):
        argv, room = self._argv, self._room()
        if mutated:
            room = os.path.join(self.workspace, ".mutant-%d" % attempt)
            shutil.rmtree(room, ignore_errors=True)
            shutil.copytree(self._room(), room, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".mutant-*", "__pycache__"))
            adapter = os.path.join(room, _subject_name(spec.language))
            wrong = _dispatcher_source(spec.language, self._dispatch,
                                       mutant=attempt)
            with open(adapter, "w", encoding="utf-8") as handle:
                handle.write(wrong)
            # THE SAME OVERRIDE AS THE REAL SUBJECT, or the mutation is not what gets served. This
            # room is a COPY of the built workspace, so the original dispatcher is already sitting in
            # it; writing the wrong one under a different name and then serving the table's name
            # would run the correct dispatcher and score the mutant as identical to the reference --
            # E3 would report that no channel discriminates, for a mutant that was never loaded.
            _, argv = shims.materialise(room, spec.language, adapter, "entry",
                                        subject_name=_subject_name(spec.language),
                                        served_name=_served_name(spec.language))
        if getattr(self._backend, "name", "") in ("docker", "remote"):
            return RemoteSubject(argv, workspace=room, backend=self._backend,
                                 timeout=PROBE_TIMEOUT)
        return Subject(argv, cwd=room, timeout=PROBE_TIMEOUT)

    def _room(self) -> str:
        """What gets pushed to the sandbox: the whole workspace.

        NOT the directory the binary sits in. A Go dispatcher lives in `<package>/frfd/` and imports
        the module's other packages, so pushing only that directory would leave go.mod and every
        imported package behind. The argv carries absolute paths that `RemoteSubject` rewrites
        against this root, so a deeper binary is still found.
        """
        return self.workspace

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

    # See Module.supports_cross_language: declared beside the `specify` that honours it, so
    # `automation.run` can refuse a cross-language run that nothing would carry through.
    supports_cross_language = True

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
        return sourcing.walk(self._index, budget, page_size=page_size,
                             memory=sourcing.batch_memory(self))

    def specify(self, candidate: Candidate, *,
                task_form: TaskForm = TaskForm.INPLACE) -> Spec:
        """One package -> what to install and how to dispatch into it.

        `target_language` prefers the configured value over the material's. The material carries one
        only when the index put it there, and `_index()` never does -- so reading the material alone
        is how a `form: cross` run emitted same-language tasks while reporting success.
        """
        self._material = self._locate(candidate)
        # A new candidate means a new subject, so the cached observer is stale. Not
        # resetting it would serve the previous candidate for the rest of a batch --
        # every task after the first describing material it was not built from.
        self._built = None
        material = self._material
        return Spec(name=_task_name(material), scale=self.name, language=material.language,
                    description=material.description, build=list(material.install),
                    invoke=["serve"], entry="entry",
                    target_language=(getattr(self, "_target_language", "")
                                     or material.target_language),
                    task_form=task_form,
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
            # ASKED FOR ENOUGH THAT 60 DISTINCT IS REACHABLE. Bounded batches exist because every
            # probe is one round trip into the sandbox, and 200 of them on a native surface was slow
            # enough to be worth capping -- but 80 asked against 60 needed demands 75% of a
            # generator's output be unique, where every other language is asked for 200 and needs the
            # same 60, a 30% bar. Measured: `xyflow` offered 120 and kept 40; `jsoncrack.com` offered
            # 60 and kept 52. Both were refused for duplication while being asked for the least.
            requested = 140 if self._material.language in ("javascript", "typescript") else PROBE_COUNT
            drawn = self._run_generator(self._material.generator, requested)
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
                drawn = self._run_generator(repaired, requested)
            except Exception as repair_exc:
                raise ValueError("package probe generator failed in the sandbox: %s; repair failed: %s"
                                 % (str(exc)[:900], str(repair_exc)[:900])) from repair_exc
        labels = None
        if isinstance(drawn, dict):
            labels = drawn.get("labels")
            drawn = drawn.get("probes")
        if isinstance(labels, list):
            labels = ["error" if label == "invalid" else label for label in labels]
        probes = _as_argument_lists(drawn)
        # TOPPED UP AGAINST THE NUMBER THE AUDIT WILL JUDGE, which is the DISTINCT count. Measuring
        # the raw count here made the battery useless exactly when it was needed: `jsoncrack.com`
        # offered 60 probes of which 52 were distinct, so `len(probes) < 60` was false, the battery
        # declined to help, and the audit then refused the package for having 52. The rescue and the
        # condemnation have to be reading the same number.
        #
        # EVERY LANGUAGE, not just javascript and typescript. The original comment justified the
        # restriction by JS packages lacking machine-readable signatures -- true, and not the only way
        # to end up short. Measured across a run of the languages this scale still needs: ruby
        # generators returned 15 and 40 distinct probes against the same bar, and were refused with
        # the battery sitting unused because of the language check rather than because of anything
        # about ruby.
        # THE AUDIT HAS TWO FLOORS, so the battery has to clear both. Filling toward the total alone
        # left the second one failing for a different reason than before: `jsoncrack.com` reached 60
        # distinct probes and was then refused with `did not cover operations with at least two
        # probes: JPathModal, JQModal, JSONCrack, TypeModal, activate, ...` -- eight operations that
        # got nothing because the loop stopped counting at sixty and they came last in the surface.
        #
        # So the per-operation floor is filled FIRST, for every operation, and only then is the total
        # topped up. Fixing this the other way round would keep re-discovering the same shape of bug:
        # a rescue that measures one thing while the condemnation measures another.
        unique_count = len(_distinct(probes))
        per_operation = _operation_counts(probes)
        names = [str(entry.get("name")) for entry in self._material.dispatch
                 if entry.get("name")]
        thin = [name for name in names if per_operation.get(name, 0) < 2]
        if unique_count < 60 or thin:
            expanded = list(probes)
            labels_out = list(labels) if labels is not None else ["valid"] * len(probes)
            # ONLY GENUINELY NEW CASES COUNT. Appending a probe the generator already emitted moves
            # the raw count and leaves the distinct count where it was, which is the same confusion
            # one layer down.
            present = {_probe_key(args) for args in expanded}
            counts = dict(per_operation)
            cases = [([], "boundary"), ([None], "error"), ([0], "boundary"), ([""], "boundary"),
                     ([[]], "error"), ([{}], "error"), ([-1], "boundary"), ([" "], "boundary"),
                     ([True], "boundary"), ([[None]], "error")]

            def offer(name: str, args: list, label: str) -> bool:
                candidate = [name] + args
                key = _probe_key(candidate)
                if key in present:
                    return False
                present.add(key)
                expanded.append(candidate)
                labels_out.append(label)
                counts[name] = counts.get(name, 0) + 1
                return True

            # PASS ONE: every operation to two distinct probes, whatever the total reaches. An
            # operation carried by one probe proves only that it can be named.
            for name in names:
                for args, label in cases:
                    if counts.get(name, 0) >= 2:
                        break
                    offer(name, args, label)
            # PASS TWO: the total, spread across operations rather than poured into the first.
            for args, label in cases:
                if len(present) >= 60:
                    break
                for name in names:
                    if len(present) >= 60:
                        break
                    offer(name, args, label)
            probes, labels = expanded, labels_out
        _audit_probe_contract(probes, self._material.dispatch, labels=labels)
        # THE CORPUS IS THE DISTINCT PROBES. The audit judges them, so freezing anything else would
        # grade a set nothing checked -- and a repeated probe costs five freeze runs to re-learn an
        # answer already held. Labels follow their probe, or a corpus would carry a valid/error
        # balance describing inputs it no longer contains.
        if labels and len(labels) == len(probes):
            paired = {}
            for args, label in zip(probes, labels):
                paired.setdefault(_probe_key(args), label)
            probes = _distinct(probes)
            labels = [paired[_probe_key(a)] for a in probes]
        else:
            probes = _distinct(probes)
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
                "The dispatch below is a JSON list of objects with keys name/module/symbol/signature "
                "and, for a statically typed package, params; iterate those objects by "
                "entry['name'], never unpack entries as (name, cases). "
                # THE TYPES ARE IN THE SURFACE AND HAVE TO BE OBEYED. Without this the model guessed
                # from the name, and a Go operation taking []byte was probed with a plain string --
                # every probe refused with `argument 0 is not a bytes`, so a dispatcher that worked
                # produced a corpus that could grade nothing.
                "When an entry has params, each param has a `kind`, and argument i must match "
                "params[i]['kind'] exactly: int -> a JSON integer; float -> a JSON number; "
                "bool -> true/false; string -> a JSON string; bytes -> a BASE64-ENCODED STRING "
                "(never a list of byte values); int_array/float_array -> a JSON array of numbers. "
                "Match the arity too: pass exactly len(params) arguments after the operation name. "
                "Example output shape: {'probes': [['op', 'x']], 'labels': ['valid']}. "
                # A GLOBAL TARGET IS THE WRONG THING TO ASK A GENERATOR FOR. "At least 60 distinct
                # probes" makes the model track a running total across a surface it is still
                # enumerating, and it does not: 22 of 24 package probe refusals were corpora that
                # deduplicated below the floor. A per-operation quota is arithmetic it can follow
                # while writing each case, and it reaches the same total by construction.
                #
                # The floor itself is not negotiable and is not a magic number: `MIN_GRADED_POINTS`
                # is 40, freeze holds three probes out for timing and discards whatever the
                # reference will not repeat, so 60 distinct is 40 plus the margin that loss takes.
                + ("Produce at least %d DISTINCT probes for EACH operation -- different arguments, "
                   "not the same call repeated. With %d operations that is %d distinct probes in "
                   "total, which is the minimum this corpus is accepted at. Two probes that differ "
                   "only in whitespace or key order count as one. " % (_per_operation_quota(dispatch),
                                                                      max(1, len(dispatch)),
                                                                      _per_operation_quota(dispatch)
                                                                      * max(1, len(dispatch)))) +
                "Every argument must be JSON-serializable (null, boolean, number, string, list, "
                "or object with string keys); never return sets, tuples, bytes, objects, or callables. "
                "Include valid, invalid and boundary cases for every operation. "
                "Return only code.\n" + surface,
                system="Define only a top-level probes(n) generator. Do not execute the package.",
                timeout=min(60, model.TIMEOUT))
            try:
                generator = validated_generator(answer)
                print("[package] received probe generator for %s" % candidate.identity, flush=True)
            except model.ModelError:
                # A transport/gateway timeout is not malformed model code. Retrying it as a
                # repair request doubles the candidate wall time and can starve a bounded roll.
                raise
            except (ValueError, SyntaxError):
                answer = model.ask("Return ONLY valid Python defining probes(n).\n" + surface,
                                   system="Define exactly probes(n).", timeout=min(60, model.TIMEOUT))
                generator = validated_generator(answer)
        operations = tuple(PackageOperation(str(entry.get("name") or ""),
                                             str(entry.get("module") or ""),
                                             str(entry.get("symbol") or ""),
                                             str(entry.get("signature") or ""),
                                             bool(entry.get("json_safe", True)),
                                             str(entry.get("klass") or ""),
                                             tuple(entry.get("params") or ()),
                                             dict(entry.get("result") or {}))
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

def _probe_key(args) -> str:
    """How two probes are told apart. -> a stable string.

    ONE DEFINITION, because three places depend on agreeing about what "the same probe" means: the
    deduplication below, the boundary battery that tops a short corpus up, and the pairing that keeps
    a label with its probe. When the battery measured raw probes while the audit measured distinct
    ones, a package offering sixty probes of which fifty-two were distinct was refused with the
    battery sitting unused -- neither number was wrong, they were answers to different questions.
    """
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def _distinct(probes: list) -> list:
    """The probes with repeats removed, in the order they were first offered.

    A CORPUS COUNTS DISTINCT INPUTS. Asking the same question twice measures nothing the first
    answer did not, so a repeat is not evidence -- it is the same evidence written down again.
    """
    seen, kept = set(), []
    for args in probes:
        key = _probe_key(args)
        if key in seen:
            continue
        seen.add(key)
        kept.append(args)
    return kept


def _operation_counts(probes: list) -> dict:
    """How many DISTINCT probes each operation carries. -> {operation: count}.

    Counted over the distinct probes for the same reason `_audit_probe_contract` judges them there:
    an operation "covered" by the same probe twice has one piece of evidence written down twice. The
    battery that tops a short corpus up needs this exact number, because the floor it has to clear
    is the one the audit will apply.
    """
    counts: dict = {}
    for args in _distinct(probes):
        if args and isinstance(args[0], str):
            counts[args[0]] = counts.get(args[0], 0) + 1
    return counts



# How many distinct probes each operation must carry for the corpus to clear its floor.
#
# `_audit_probe_contract` wants 60 distinct overall and at least two per operation. Asked as a
# global number the generator does not hit it; asked per operation it is arithmetic it can follow
# while writing each case. Three is the floor per operation even on a wide surface, because two is
# the bare coverage minimum and leaves nothing for freeze to discard.
def _per_operation_quota(dispatch) -> int:
    operations = max(1, len(dispatch))
    return max(3, -(-60 // operations))


def _audit_probe_contract(probes: list, dispatch: tuple, *, labels=None) -> None:
    """Reject generator output that cannot cover a package contract honestly.

    EVERY FLOOR IS APPLIED TO THE DISTINCT PROBES, which is both more forgiving and stricter than
    counting what was offered.

    More forgiving: a generator that returns two hundred probes of which seventy are distinct used
    to be refused outright for "too many duplicate probes", and seventy distinct probes is a usable
    corpus. That refusal was eight of this scale's ten probe-stage losses.

    Stricter, and this is the half that was wrong: `counts` incremented per probe, so an operation
    "covered" by the same probe twice satisfied the two-probe floor. The comment beside that floor
    already said what it wanted -- "two DISTINCT probes are the minimum evidence that its behaviour,
    rather than just its dispatch wrapper, is being graded" -- and the code was not checking it.
    """
    names = {str(entry.get("name")) for entry in dispatch if entry.get("name")}
    for index, args in enumerate(probes):
        if not args or not isinstance(args[0], str):
            raise ValueError("package probe %d does not start with an operation name" % index)
        if args[0] not in names:
            raise ValueError("package probe %d names unknown operation %r" % (index, args[0]))

    unique = _distinct(probes)
    if len(unique) < 60:
        raise ValueError(
            "package generator returned only %d distinct probes; need at least 60 (%d offered)"
            % (len(unique), len(probes)))
    counts = {name: 0 for name in names}
    for args in unique:
        counts[args[0]] += 1
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



def _slug(text: str) -> str:
    """A task-name fragment: lower kebab-case, no camel humps, no stray punctuation.

    A NAME IS READ BY PEOPLE, and half of ours were not readable as one thing: `interview-BubbleSort`
    and `librec-weightedcMean` mix a kebab-case repository with a camel-case symbol, so the same
    corpus spells identifiers three ways. The reference benchmarks this factory is measured against
    are uniformly kebab -- `cranelift-codegen-opt`, `libexpat-to-x86asm` -- and matching that costs
    one function.

    Camel humps become separators, so `BubbleSort` is `bubble-sort` and `weightedcMean` is
    `weightedc-mean`; underscores and dots do the same. Runs of separators collapse.
    """
    out = []
    for index, char in enumerate(str(text)):
        if char in "_. /":
            out.append("-")
            continue
        if char.isupper() and index and (str(text)[index - 1].islower() or str(text)[index - 1].isdigit()):
            out.append("-")
        out.append(char.lower())
    joined = "".join(out)
    while "--" in joined:
        joined = joined.replace("--", "-")
    return joined.strip("-")


def _task_name(material: Material) -> str:
    """The package, and what is being asked of it. Not the revision.

    THE COMMIT DOES NOT BELONG IN A NAME, which the repo scale already established and this one
    missed: `gonum@8d8e8a102004-faster` is what a person has to read, type and compare, and two
    revisions of one package produce names differing by twelve hex digits for no gain. The revision
    is pinned in provenance, where it is exact and nobody has to read it.
    """
    stem = material.identity.rsplit("/", 1)[-1]
    stem = stem.split("@", 1)[0] or stem
    stem = _slug(stem)
    return "%s-rewrite" % stem if material.target_language else "%s-faster" % stem

def _install_commands(language: str) -> tuple:
    """How a package of `language` obtains its own dependencies. -> argv lists, run in the module.

    WHAT DECIDES WHAT BELONGS HERE: the install must land INSIDE the workspace tree. The tree is
    pulled back out of the build sandbox and pushed again into a fresh one for the freeze, so
    anything written outside it -- `~/.npm`, `~/.cargo`, a global gem path, the Go module cache --
    is gone by the time the subject is served. `node_modules/` and `vendor/bundle/` are in the tree
    and survive; a module cache in `$HOME` does not.

    That is also why this table is short rather than one entry per language:

    * javascript / typescript -- `node_modules/` is in the tree. THE BLOCKER, measured: every one of
      2882 package checkouts had none, so any package importing anything at all failed to load.
    * ruby -- `vendor/bundle` is in the tree, with `--local` refused so it may actually fetch.
    * go -- ABSENT ON PURPOSE. Its dependencies reach the subject through the compiled binary, which
      the pull already carries, and `go build` fetches them itself. Adding `go mod vendor` here would
      rewrite the manifest of a scale that has nine attested tasks working exactly as it is.
    * rust -- ABSENT, and not because it works. Its 29 build failures are OURS: `_static_rust`
      discards the `klass` the miner gives it and emits unqualified calls, so a mined METHOD becomes
      `cannot find function `rfind` in this scope` at `subject.rs:271`. The same generated line failed
      across kalker, memchr, faer-rs and TheAlgorithms/Rust -- one template bug, not four packages.
      An install would not move that.
    * python -- ABSENT. It is the one language that already works here (36 attested tasks), and it is
      four times over the per-language cap, so widening its supply serves no goal.

    BEST EFFORT AT THE CALL SITE, not here: see `Observer.build`. These commands may fail without
    refusing the candidate, because a broken devDependency must not condemn a package whose runtime
    imports are fine -- the freeze's "did the subject ever answer" gate is what judges that, and it
    reports the subject's own error rather than an installer's exit code.
    """
    # NEVER `cmd | tail`: A PIPELINE'S STATUS IS ITS LAST COMMAND'S. `tail` always succeeds, so
    # `npm ci ... | tail -20 || npm install ...` reports success however npm did -- which broke both
    # halves at once. The `||` fallback became unreachable, so a stale lockfile was never retried, and
    # `done.ok` was always true, so the caller's failure report never printed. The first run of this
    # showed `install-failure prints: 0` beside three subjects that still could not resolve their
    # imports, and the two facts could not be reconciled because one of them was a lie.
    #
    # So output goes to a file, the status is captured explicitly, and the tail is printed afterwards.
    # Bounded output AND a truthful exit code; the pipe bought the first by destroying the second.
    if language in ("javascript", "typescript"):
        # THE LOCKFILE CHOOSES THE INSTALLER, because npm is not a superset of the others. Counted
        # across the 1197 js/ts checkouts on disk:
        #
        #     package-lock.json  603     npm ci works
        #     yarn.lock          218     npm ci refuses: no package-lock
        #     pnpm-lock.yaml     108     npm ci refuses, and `workspace:` deps make npm fail
        #                                 EUNSUPPORTEDPROTOCOL
        #     no lockfile        300     npm ci refuses outright
        #
        # So `npm ci` alone reaches half the supply, and `npm ci || npm install` reaches most of it
        # while resolving yarn and pnpm trees from scratch -- which is where ERESOLVE came from.
        #
        # `--legacy-peer-deps` IS THE POINT OF THE FALLBACK. npm 7+ treats a peer-dependency conflict
        # as fatal, and a repository pinned to an arbitrary commit has them routinely; the first run
        # that could report its own failures showed ERESOLVE as the top cause. We are not publishing
        # this tree, only importing from it, so a peer-range disagreement is not a reason to refuse a
        # package.
        #
        # SCRIPTS ARE NOT SUPPRESSED. `--ignore-scripts` would block the `prepare` step a package uses
        # to build itself -- the same mistake as `npm install --offline` against an empty cache:
        # removing what the program needs and then blaming the program.
        #
        # `command -v` FIRST for each, so "the toolchain is absent" stays a different message from
        # "the install failed". Those need different fixes and the log has to say which.
        return (["sh", "-c",
                 "L=/tmp/frf-install.log; "
                 "if [ -f pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then "
                 "  pnpm install --no-frozen-lockfile >$L 2>&1; "
                 "elif [ -f yarn.lock ] && command -v yarn >/dev/null 2>&1; then "
                 "  yarn install --non-interactive >$L 2>&1; "
                 "elif command -v npm >/dev/null 2>&1; then "
                 "  if [ -f package-lock.json ]; then "
                 "    npm ci --no-audit --no-fund --legacy-peer-deps >$L 2>&1 || "
                 "    npm install --no-audit --no-fund --legacy-peer-deps >$L 2>&1; "
                 "  else "
                 "    npm install --no-audit --no-fund --legacy-peer-deps >$L 2>&1; "
                 "  fi; "
                 "else echo 'no node package manager in this sandbox'; exit 127; fi; "
                 "status=$?; tail -25 $L; exit $status"],)
    if language == "ruby":
        # A GEM DECLARES ITS DEPENDENCIES IN THE GEMSPEC, NOT IN A GEMFILE, and this used to give up
        # unless a Gemfile was already there: `if [ ! -f Gemfile ]; then exit 0; fi`. The material
        # this scale sources is published gems, and a library normally ships a `.gemspec` with
        # `add_dependency` lines and no Gemfile at all -- Gemfiles belong to applications. So nothing
        # was installed, the subject's own `require` raised `LoadError`, and the shim died before it
        # could answer. Measured: 17 of one batch's freeze refusals, every one ruby.
        #
        # `gemspec` IN A GENERATED GEMFILE is bundler's own mechanism for exactly this: it reads the
        # gemspec in the current directory and resolves what it declares. Written only when there is
        # no Gemfile, so a project that has one keeps it.
        #
        # Installed into `vendor/bundle` because only what is INSIDE the tree survives the push to
        # the sandbox that freezes it; the system gem home does not travel.
        return (["sh", "-c",
                 "L=/tmp/frf-install.log; "
                 "if ! command -v bundle >/dev/null 2>&1; then "
                 "  echo 'bundler is not in this sandbox' >$L; tail -5 $L; exit 0; fi; "
                 "if [ ! -f Gemfile ] && ls *.gemspec >/dev/null 2>&1; then "
                 "  printf 'source \"https://rubygems.org\"\\ngemspec\\n' > Gemfile; fi; "
                 "if [ ! -f Gemfile ]; then echo 'no Gemfile and no gemspec' >$L; tail -5 $L; exit 0; fi; "
                 "bundle config set --local path vendor/bundle >$L 2>&1; "
                 "bundle install --jobs 4 --retry 1 >>$L 2>&1 || "
                 "bundle install --jobs 4 --retry 1 --no-deployment >>$L 2>&1; "
                 "tail -20 $L; "
                 "[ -d vendor/bundle ] || echo '[install] vendor/bundle absent afterwards'; "
                 # Best effort, as for the other languages: a broken development dependency must not
                 # refuse a gem whose runtime requires are fine. The freeze decides that, and it now
                 # sees the subject's own error rather than a dead process.
                 "exit 0"],)
    return ()


def _subject_name(language: str) -> str:
    """What the subject file is called, asked of the shim that will serve it.

    The scale used to keep a second copy of this ("subject.js" if native else
    "subject.py"), which is the kind of duplicated truth that goes stale the
    moment a language is added -- and it already disagreed with the TypeScript
    shim, which serves `subject.ts`.
    """
    # `.cjs` FOR JAVASCRIPT, BECAUSE THIS SUBJECT IS OURS. The table says `subject.js`, which is right
    # for a MINED function -- that is usually ESM (`export function ...`) and renaming it would make
    # Node parse it as CommonJS and fail on the export. What the package scale writes is not mined
    # source: it is the dispatcher `dispatch.source()` generates, in CommonJS (`exports.entry = ...`).
    # A package declaring `"type": "module"` makes every neighbouring `.js` file ESM, so that
    # dispatcher dies on its own first line with `exports is not defined in ES module scope`.
    # Measured: 50 of one batch's javascript package candidates, every one of them.
    #
    # `.cjs` is the escape hatch the shim itself already uses (`serve.cjs`): CommonJS whatever the
    # package declares. TypeScript is NOT included -- its dispatcher is written as `.ts` and compiled
    # by `tsc --module commonjs`, so what Node loads is already CommonJS and renaming the input would
    # only confuse the compiler about what it was given.
    if language == "javascript":
        return "subject.cjs"
    # TYPESCRIPT HAS THE SAME DEFECT, and excluding it was a mistake I made on a wrong reading: I
    # argued that `tsc --module commonjs` makes the OUTPUT CommonJS so nothing was at risk. The
    # content being CommonJS is exactly the problem -- Node decides how to PARSE a file from its
    # extension, not its content, so `subject.js` under `"type": "module"` is read as ESM and the
    # CommonJS the compiler just wrote fails on `exports`. TypeScript's own answer is the `.cts`
    # extension, which `tsc` compiles to `.cjs`; see `_served_name` for the other half.
    if language == "typescript":
        return "subject.cts"
    return shims.TEMPLATES[language].subject


def _served_name(language: str) -> str:
    """What the runtime loads, when the compiler renames what we wrote. -> a filename, or "".

    Only typescript needs this: `tsc` derives its output name from the input's extension, so handing it
    `subject.cts` produces `subject.cjs`, and the run argv has to name the second. Empty for everything
    else, which leaves each shim's own declared name alone.
    """
    return "subject.cjs" if language == "typescript" else ""


def _dispatcher_source(language: str, dispatch: dict,
                       *, mutant: int | None = None) -> str:
    """The `entry` seam for `language`, real or deliberately wrong.

    Generating the mutant rather than appending to the real source is what keeps
    the mutation gate honest: appended Python source was a syntax error in every
    language but Python, so the probe "caught" mutants it never had to reason
    about.
    """
    return call_dispatch.source(language, dispatch, mutant=mutant)
