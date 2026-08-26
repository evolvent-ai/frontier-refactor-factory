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
import subprocess
import uuid
from dataclasses import dataclass

from ..core import integrity
from ..core.scale import Candidate, Spec
from ..observe import coverage
from ..observe.call import shims
from ..observe.call.runner import RemoteSubject, Subject
from ..observe.probes.schema import Schema, sample

# How many probes a corpus is drawn with. Above the pipeline's floor by a margin: on this seam a
# probe is worth one point, three are held out for timing, and a corpus that only just clears the
# floor before those are removed does not clear it afterwards.
# Keep the initial corpus bounded: each probe is frozen five times through a remote process seam.
# Adequacy may request more probes later, but starting with 60 makes one candidate cost 300 E2B
# calls before we know whether it is stable.
PROBE_COUNT = 60

# How long a subject may take to compile. Generous enough for an optimising compiler on a real
# translation unit, bounded so that a build which will never finish does not stall a batch.
BUILD_TIMEOUT = 600.0

# How long one probe may take. Real mined material makes this necessary rather than tidy: a naive
# `fibonacci_recursion` from PyPI is exponential in its argument, and at the sizes the schema draws
# it never returns at all. Without a bound the batch stops with no output and no error, and nothing
# says which candidate did it. A subject too slow to answer cannot be graded, which is the
# material's fault and an ordinary refusal.
# A probe may involve a cold import or a deliberately boundary-heavy package operation.
# Keep the guard configurable, but do not reject otherwise usable E2B subjects after a few seconds.
PROBE_TIMEOUT = float(os.environ.get("FRF_PROBE_TIMEOUT", "120"))

# How many DIFFERENT perturbations E3 may try before concluding that a subject cannot be
# distinguished by this crude a mutation. More than one because an edit can be real and still
# semantically inert -- see `mutate` -- and one such edit should not decide the verdict for a
# candidate that is otherwise perfectly gradeable.
MUTATION_ATTEMPTS = 4


class BuildFailed(RuntimeError):
    """The subject could not be compiled -- material's fault, not the wire's.

    Distinct from `SubjectFailed`, which is a subject that built and then would not speak. Merging
    them would send a repair loop to inspect the protocol every time a candidate had a syntax error.
    """

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
        probes = []
        # Reserve a deterministic semantic prefix. Pure random draws routinely miss the
        # distinguishing branch (for example, two random strings are almost never anagrams),
        # allowing a constant answer to pass a superficially large corpus.
        for i in range(count):
            shape = self.shapes[i % len(self.shapes)]
            args = sample(self.schema, seed=i, shape=shape)
            if i < 8:
                args = self._semantic(args, i, shape)
            probes.append(args)
        return probes

    def _semantic(self, args: list, index: int, shape: dict) -> list:
        strings = [n for n, p in enumerate(self.schema.params) if p.kind == "string"]
        if len(strings) >= 2:
            left, right = strings[:2]
            cases = (("", ""), ("a", "a"), ("ab", "ba"), ("ab", "aa"),
                     ("a", ""), ("", "a"), ("listen", "silent"), ("listen", "enlist"))
            args[left], args[right] = cases[index]
        for n, param in enumerate(self.schema.params):
            if param.kind == "bool" and index < 2:
                args[n] = bool(index)
            elif param.kind in ("int", "float") and index < 4:
                args[n] = (param.low, 0, param.high, 1)[index]
            elif param.kind in ("int_array", "float_array") and index < 4:
                size = int(shape.get(param.size, 0)) if isinstance(param.size, str) else int(param.size or 0)
                args[n] = [0] * min(size, (0, 1, 4, 16)[index])
        return args


class Observer:
    """The call seam, bound to a workspace where the subject is served.

    Everything a shared stage needs from a seam is here and is deliberately small: start the
    subject, measure its coverage, list what it must not reach, and say whether timing is isolated.
    """

    def __init__(self, workspace: str, material: Material, *, backend=None) -> None:
        self.workspace = workspace
        self.material = material
        # Which sandbox the subject runs in, so that `isolation()` can answer from what is actually
        # in force rather than from a hope. None means this process, which isolates nothing.
        self._backend = backend
        # Set by `_restricted` when the wrapper is genuinely applied, and read by `isolation`. A
        # flag rather than an inference from the backend's name: naming a container says the
        # defence is POSSIBLE, and only applying it makes the defence real.
        self._isolated = False
        self._argv: list = []

    def build(self, spec: Spec) -> None:
        """Put the subject and its shim where they can be served from, and compile if the language
        needs compiling.

        The build runs HERE rather than at first call, so that a subject which does not compile is a
        failure of this stage -- named, with the compiler's own message -- instead of a subject that
        later "exits without answering", which is the same fault wearing a disguise that sends the
        repair loop to look at the wire.
        """
        build, argv = shims.materialise(self.workspace, spec.language,
                                        self.material.source_path, self.material.symbol)
        if self._backend is not None and getattr(self._backend, "name", "") != "local-process":
            # Production references are built in the same sandbox image that will freeze them.
            # Keep a per-observer remote directory so concurrent candidates never share compiler
            # outputs; pull it back because RemoteSubject stages this host workspace for each run.
            remote = "/tmp/frf-build-%s" % uuid.uuid4().hex[:12]
            self._backend.push(self.workspace, remote)
            for command in build:
                remote_command = [part.replace(self.workspace, remote)
                                  if isinstance(part, str) else part for part in command]
                done = self._backend.run(remote_command, workdir=remote, timeout=BUILD_TIMEOUT)
                if not done.ok:
                    raise BuildFailed("%s did not build: %s" %
                                      (self.material.identity, done.tail(800)))
            self._backend.pull(remote, self.workspace)
            self._argv = [part.replace(self.workspace, remote)
                          if isinstance(part, str) else part for part in argv]
            # Subject() is not used for remote execution; RemoteSubject rewrites this host path
            # back to its own staging directory. Keep host paths here for that contract.
            self._argv = argv
            return
        for command in build:
            done = subprocess.run(command, cwd=self.workspace, capture_output=True, text=True,
                                  timeout=BUILD_TIMEOUT)
            if done.returncode != 0:
                raise BuildFailed("%s did not build: %s"
                                  % (self.material.identity,
                                     (done.stderr or done.stdout).strip()[-800:]))
        self._argv = argv

    def _restricted(self, argv: list) -> list:
        """The argv, wrapped so the subject runs unprivileged and cannot fork a fleet.

        Applied only where it means something. On a backend that shares this machine the wrapper
        would be theatre -- the two sides still see the same kernel -- and on a host with neither
        `setpriv` nor `su` it cannot be applied at all. Both cases leave `_isolated` False, which is
        what makes E6 report INCONCLUSIVE rather than certifying a defence that is not in force.

        BOTH SIDES ARE WRAPPED OR NEITHER IS. The reference and the candidate must start the same
        way, or the comparison measures the wrapper rather than the subjects.
        """
        if getattr(self._backend, "name", "") not in ("docker", "remote"):
            return argv
        try:
            wrapped = integrity.restricted_argv(argv)
        except LookupError:
            return argv
        self._isolated = True
        return wrapped

    def subject(self, spec: Spec | None = None, *, mutated: bool = False,
                attempt: int = 0) -> Subject:
        """The subject, served. `mutated` serves a perturbed one instead.

        THE MUTANT IS WHAT MAKES E3 MEAN ANYTHING. That check asks whether the verifier would notice
        a submission that behaves differently, and it can only answer by being shown one. An
        observer that accepted `mutated` and ignored it would serve the reference twice: the
        comparison would find no divergence, and the check would report INCONCLUSIVE forever --
        which is the honest verdict for a mutation that never happened, and useless.

        Served from a separate directory so that the reference's own workspace is never edited. The
        alternative -- rewriting the subject in place and putting it back -- leaves a corrupted
        subject behind on any failure between the two.
        """
        argv, room = ((self._argv, self.workspace) if not mutated
                      else self._mutant(attempt))
        # IN A SANDBOX, THE SUBJECT RUNS IN THE SANDBOX. Serving it here while a container was
        # selected froze an expectation against the factory host -- a program with this machine's
        # interpreter and this machine's installed packages, which is not what the shipped image
        # contains. What travels is the workspace: one subject file and one shim, so the transfer
        # is two small files rather than a checkout.
        if getattr(self._backend, "name", "") in ("docker", "remote"):
            return RemoteSubject(argv, workspace=room, backend=self._backend,
                                 timeout=PROBE_TIMEOUT)
        return Subject(self._restricted(argv), cwd=room, timeout=PROBE_TIMEOUT)

    def _mutant(self, attempt: int = 0) -> tuple:
        """A copy of the workspace whose subject has been perturbed. -> (argv, cwd).

        The perturbation is applied to the SOURCE and the result is rebuilt, because a compiled
        language has nothing else to perturb. It is deliberately crude -- see `mutate` -- since what
        E3 needs is any provable difference in behaviour, not a realistic wrong answer.
        """
        room = os.path.join(self.workspace, ".mutant-%d" % attempt)
        # E3 mutations are generated in a separate workspace from the actual source.
        if os.path.isdir(room):
            shutil.rmtree(room, ignore_errors=True)
        os.makedirs(room, exist_ok=True)

        original = open(self.material.source_path, encoding="utf-8", errors="replace").read()
        perturbed = os.path.join(room, os.path.basename(self.material.source_path))
        with open(perturbed, "w", encoding="utf-8") as handle:
            handle.write(mutate(original, self.material.language, self.material.symbol,
                                attempt))

        build, argv = shims.materialise(room, self.material.language, perturbed,
                                        self.material.symbol)
        for command in build:
            done = subprocess.run(command, cwd=room, capture_output=True, text=True,
                                  timeout=BUILD_TIMEOUT)
            if done.returncode != 0:
                # A mutant that will not build is not a verdict about the verifier. Falling back to
                # the reference makes E3 report INCONCLUSIVE, which is the truth: nothing was shown.
                return self._argv, self.workspace
        return argv, room

    def coverage(self):
        return coverage.backend_for(self.material.language)

    def forbidden_references(self, spec: Spec) -> list:
        """What the subject tree reaches that it must not. Checked mechanically, never by judgement.

        Reads the workspace rather than returning the task's ban list. The distinction is the whole
        check: a list of forbidden names is what the task FORBIDS, and returning it here would make
        the evidence check report a failure for every task that has a rule. What E6 needs is what
        was actually FOUND, which requires opening the files.
        """
        found = integrity.inspect(self.workspace, tuple(self.material.forbidden))
        return [str(hit) for hit in found.hits]

    def isolation(self):
        """How the two sides are kept apart while one is timed -- reported, never assumed."""
        return integrity.isolation_for(self._backend, applied=self._isolated)

    def isolated(self) -> bool:
        """Whether timing runs with the two sides separated.

        Answered from the backend rather than optimistically: on a local backend this is False, the
        delegation check reports INCONCLUSIVE, and that is the correct verdict when work handed to
        another process would be invisible to the clock. Returning a constant True here would make
        E6 report HOLDS for a defence that was never in force.
        """
        return self.isolation().enforced


class Module:
    """The module scale. Four answers; the pipeline does the rest."""

    name = "module"

    def __init__(self, index=None, workspace: str = "", *, observer=None, backend=None) -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "module")
        self._observer = observer
        # Passed through to the Observer so the delegation check can report what is really in
        # force. Threaded rather than looked up globally: two scales in one batch may legitimately
        # run in different sandboxes.
        self._backend = backend
        self._material: Material | None = None
        # The observer, once built. See observe().
        self._built = None

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

        # Widening indexes (GitHub -> functions) do real checkout and AST work per row. Keep the
        # source page close to the requested batch size so budget=1 does not expand fifty repos.
        page_size = 4 if getattr(self._index, "name", "") == "github-functions" else 50
        return sourcing.walk(self._index, budget, page_size=page_size)

    def specify(self, candidate: Candidate) -> Spec:
        """One candidate -> what to build and how to call it."""
        self._material = self._locate(candidate)
        # A new candidate means a new subject, so the cached observer is stale. Not
        # resetting it would serve the previous candidate for the rest of a batch --
        # every task after the first describing material it was not built from.
        self._built = None
        material = self._material
        return Spec(name=_task_name(material), scale=self.name, language=material.language,
                    description=material.description,
                    invoke=["serve", material.symbol], entry=material.symbol,
                    environment={"subject_path": os.path.join(
                                     self._workspace, shims.load(material.language).subject),
                                 "forbidden": list(material.forbidden)})

    def observe(self):
        if self._observer is not None:
            return self._observer
        if self._material is None:
            raise RuntimeError("observe() was asked for before specify() chose a subject")
        # CACHED, not constructed per call. The pipeline builds through one reference to the
        # observer and then freezes through another, so a fresh instance here would be an observer
        # that was never built -- its argv empty, failing inside Popen with an IndexError that says
        # nothing about the cause.
        if self._built is None:
            self._built = Observer(self._workspace, self._material, backend=self._backend)
        return self._built

    def probes(self, spec: Spec) -> ProbeSource:
        if self._material is None:
            raise RuntimeError("probes() was asked for before specify() chose a subject")
        from dataclasses import replace
        kinds = [param.kind for param in self._material.schema.params]
        self._spec = replace(spec, notes=(spec.notes or "") +
                             " probe contract: semantic prefix covers empty/equal/reordered/mismatch/boundary cases; "
                             "parameter kinds=%s" % ",".join(kinds))
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
    """Stable and collision-free: repository slug plus symbol.

    The symbol alone is not a valid task identity: hundreds of repositories contain `sort` or
    `parse`, and emitting them into one directory would overwrite an earlier task.
    """
    identity = material.identity
    if identity.startswith("github:"):
        rest = identity[len("github:"):].split("@", 1)[0]
        repo = rest.rsplit("/", 1)[-1]
        return "%s-%s" % (repo.lower(), material.symbol.replace("_", "-").replace(".", "-"))
    return material.symbol.replace("_", "-").replace(".", "-").lower()


# What a perturbation looks like, per language family. Deliberately crude: E3 does not need a
# realistic wrong answer, it needs a PROVABLE difference in behaviour, and the check establishes
# that difference by comparing observations rather than by trusting this table.
#
# Applied to the source as text because a compiled subject has nothing else to perturb, and because
# a factory that parsed each language in order to mutate it would have re-acquired exactly the
# per-language knowledge the wire exists to avoid.
_PERTURBATIONS = (
    # Arithmetic first: it changes a returned value without changing control flow, so a subject
    # that computes anything at all will diverge and still answer.
    ("+", "-"),
    ("*", "+"),
    (">=", ">"),
    ("<=", "<"),
    ("==", "!="),
    (" and ", " or "),
    # THE BARE COMPARISONS, which is what a search or a selection is made of. Without them
    # `find_min_max` -- whose entire logic is `element < minimum` and `element > maximum` -- offered
    # exactly one mutable site, an initialiser that computes the same answer either way, so the
    # subject was reported unperturbable when in fact every line of it was a comparison. Ordered
    # after the two-character forms so that `>=` is matched as `>=` rather than as `>` followed by a
    # stray `=`.
    (" < ", " > "),
    (" > ", " < "),
    # INDEXING AND SLICING, which is what real code does when it does no arithmetic at all. The
    # first four were enough for subjects written for this factory and left a great many mined
    # functions unperturbable -- a routine that filters a list and returns `xs[-1]` contains not one
    # of them, so its mutant was identical, E3 said INCONCLUSIVE, and the candidate was refused for
    # a gap in this table rather than for anything true about the material.
    ("[-1]", "[0]"),
    ("[0]", "[-1]"),
    ("[1:]", "[:-1]"),
    (".sort()", ".reverse()"),
    ("sorted(", "reversed("),
    ("min(", "max("),
    ("max(", "min("),
    ("len(", "id("),
)


def mutate(source: str, language: str, symbol: str = "", attempt: int = 0) -> str:
    """One small change to a subject's source, chosen so the result still compiles.

    Returns the source unchanged when nothing matched, which is not a failure: the mutant then
    behaves identically, the comparison finds no divergence, and E3 reports INCONCLUSIVE. That is
    the honest outcome for a subject nobody could perturb this way, and it is why the check asks
    whether the observation MOVED rather than inferring blindness from a score.

    THE CHANGE MUST LAND IN THE FUNCTION BEING GRADED, which `symbol` is for. Perturbing the first
    `+` in the FILE was the obvious implementation and it is wrong on real material: a mined module
    holds a dozen functions, only one of them is the subject, and the first `+` is almost always in
    a different one -- or in an import, or a docstring. The mutant then behaves identically, E3
    reports INCONCLUSIVE, and the candidate is refused for a defect in this function rather than in
    the material. Measured on a real batch, that was two refusals in three.

    Located by text rather than by parsing, because a factory that parsed each language in order to
    mutate it would have re-acquired exactly the per-language knowledge the wire exists to avoid.
    The window is from the symbol's definition to the next line that starts in the first column,
    which is where a function ends in every language whose blocks are indented, and is a harmless
    over-approximation in the braced ones.
    """
    start, end = _window_of(source, symbol)
    if language.lower() in ("python", "py") and attempt == 0 and symbol:
        newline = source.find("\n", start)
        if newline != -1:
            indent = "    "
            return source[:newline + 1] + indent + "return None\n" + source[newline + 1:]
    # PAST THE SIGNATURE. A perturbation that lands on the definition line renames the function --
    # `min(` -> `max(` turns `find_min_max` into `find_min_min` -- and the shim then cannot find the
    # symbol it was told to serve. The mutant dies on import, which is not a difference in
    # behaviour but a broken build, and it arrives as an unclassified failure counted as OURS.
    signature_end = source.find("\n", start)
    if signature_end != -1:
        start = signature_end + 1
    # EVERY PLACE A PERTURBATION COULD LAND, in a stable order, so that `attempt` selects among
    # them. Enumerating the sites rather than the RULES is what makes the retry work: a subject
    # containing one `[0]` and six `+` offers seven distinct mutants, where counting rules would
    # have offered two.
    sites = []
    if language.lower() in ("python", "py"):
        at = source.find("return ", start, end)
        while at != -1:
            sites.append((at, "return ", "return None # "))
            at = source.find("return ", at + 1, end)
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        # JS/TS modules commonly use expression-bodied exports, so the generic operator table can
        # miss the subject entirely. Replacing a subject return expression is a compiling semantic
        # mutant; changing `.map(` to `.filter(` is a second independent perturbation for array
        # workloads when no return keyword exists in the selected window.
        at = source.find("return ", start, end)
        while at != -1:
            sites.append((at, "return ", "return null /* mutant */; "))
            at = source.find("return ", at + 1, end)
        at = source.find(".map(", start, end)
        while at != -1:
            sites.append((at, ".map(", ".filter("))
            at = source.find(".map(", at + 1, end)
    for original, replacement in _PERTURBATIONS:
        at = source.find(original, start, end)
        while at != -1:
            sites.append((at, original, replacement))
            at = source.find(original, at + 1, end)
    # Some valid functions (for example a pure membership predicate) contain none of the
    # expression operators above.  For Python, replacing a return expression with ``None`` is a
    # deliberately crude but always-compiling semantic mutation and gives the evidence check an
    # actual changed observation to test.
    # By position, so the first attempt is the earliest edit and the order does not depend on how
    # the table happens to be written.
    # Prefer language-specific semantic edits. Generic operator positions are useful fallback, but
    # sorting them ahead can spend all mutation attempts on inert helpers in JS modules.
    if language.lower() not in ("javascript", "typescript", "js", "ts"):
        sites.sort()
    if attempt >= len(sites):
        return source
    index, original, replacement = sites[attempt]
    # One occurrence, not all of them. Replacing every `+` in a file tends to produce something that
    # does not compile, and a mutant that does not build demonstrates nothing.
    return source[:index] + replacement + source[index + len(original):]


def _window_of(source: str, symbol: str) -> tuple:
    """Where in the file the named function lives. -> (start, end) offsets over the whole file.

    Falls back to the whole file when the symbol cannot be found, which keeps a subject written for
    this factory -- one function, called `entry` -- working exactly as before.
    """
    if not symbol:
        return (0, len(source))
    for marker in ("def %s(" % symbol, "func %s(" % symbol, "fn %s(" % symbol,
                   "function %s(" % symbol, "%s(" % symbol,
                   "%s = (" % symbol, "%s = async (" % symbol):
        opened = source.find(marker)
        if opened == -1:
            continue
        body = source.find("\n", opened)
        if body == -1:
            return (opened, len(source))
        # The end of the definition: the next line that begins in the first column. A blank line
        # does not end it, and neither does a decorator or a comment at the same indent.
        closed = len(source)
        offset = body + 1
        while offset < len(source):
            newline = source.find("\n", offset)
            line = source[offset:newline if newline != -1 else len(source)]
            if line.strip() and not line[:1].isspace() and not line.startswith(("}", ")")):
                closed = offset
                break
            if newline == -1:
                break
            offset = newline + 1
        return (opened, closed)
    return (0, len(source))
