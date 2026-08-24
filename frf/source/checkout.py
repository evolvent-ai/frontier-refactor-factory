"""Turning a repository into something the repo scale can specify.

GitHub search lists REPOSITORIES. The repo scale needs a checkout at a pinned commit, a way to
build it, a way to invoke what it built, and a corpus of scenarios. None of that is published
anywhere, so this is the adapter -- the same role `functions.py` plays for the module scale, and for
the same reason: an index says what EXISTS, and a scale needs what to DO with it.

WHAT IS DECIDED MECHANICALLY. Which build system a repository uses is written on its disk: a
`Cargo.toml` means cargo, a `go.mod` means go build, a `Makefile` means make. That is a table, not a
judgement, and it is the whole of what this module infers. What it does NOT infer is what the
program does or which of its behaviours are worth grading -- that comes from running it.

WHERE SCENARIOS COME FROM, and the trap that shapes this file. A repository's own tests already
encode what its authors consider its behaviour, so lifting their commands gives a corpus that is
about the program rather than about anyone's idea of it. But a test script is mostly shell: it makes
directories, writes files, sets variables, and calls the program a few times. Lift it wholesale and
most graded steps record what `/bin/sh` did -- the task then grades the host's shell and passes every
other check, which is exactly the failure E5 exists to catch.

So the split is enforced HERE rather than left to the checker: a step that does not invoke the
program goes into the FIXTURE, and only invocations are graded. E5 then has nothing to find, which
is the point -- a check that keeps failing on material you keep shipping is a check nobody reads.

TWO SHAPES OF TASK COME OUT OF ONE REPOSITORY.

    same-language     "make this faster". The solver gets the source and optimises in place.
    cross-language    "reimplement this in Go". The solver gets the behaviour and nothing else.

They differ in one field here -- `target_language` -- and in one thing about the image, which is
that a cross-language task ships without the source language's toolchain. That is not a rule in the
statement, because a rule in a statement is a request; it is the absence of a compiler, which is a
fact about the environment. A verifier watching four channels cannot tell what language produced a
binary, so removal is the only form of the requirement that cannot be ignored.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

from ..core.scale import Candidate

# How long a clone or a build may take. Generous: a real repository with native dependencies takes
# minutes. Bounded, because a build that will never finish must not hold a batch.
CLONE_TIMEOUT = 900.0
BUILD_TIMEOUT = 1800.0

# How long one lifted invocation may take while its behaviour is being learned. A program that takes
# longer than this to answer `--help` is not one this factory can grade in a reasonable batch.
PROBE_TIMEOUT = 60.0

# A marker file -> how that project builds and where the result lands. Data rather than a chain of
# conditionals, in the same spirit as the shim table: "which build systems are understood" is a
# question with a printable answer, and adding one is a row.
#
# `{name}` is the repository's own name, which is what these tools call their output by default.
BUILDERS = (
    ("Cargo.toml", "rust", (("cargo", "build", "--release"),), "target/release/{name}"),
    ("go.mod", "go", (("go", "build", "-o", "{name}", "./..."),), "{name}"),
    ("CMakeLists.txt", "cpp", (("cmake", "-B", "build"), ("cmake", "--build", "build")),
     "build/{name}"),
    ("Makefile", "c", (("make",),), "{name}"),
    ("configure", "c", (("./configure",), ("make",)), "{name}"),
    # Interpreted projects build nothing; what matters is finding the entry point, which is done
    # separately because a Python project can be invoked half a dozen ways.
    ("pyproject.toml", "python", (), ""),
    ("setup.py", "python", (), ""),
    ("package.json", "javascript", (), ""),
)

# Where a program's own tests tend to live. Used only to find scripts worth reading for scenarios;
# nothing here executes them, because a project's test suite is arbitrary code.
TEST_DIRECTORIES = ("tests", "test", "t", "spec", "testdata")


@dataclass
class Checkout:
    """One repository, cloned and inspected. Everything the repo scale asks for."""

    identity: str
    root: str
    language: str
    commit: str
    description: str = ""
    build: list = field(default_factory=list)
    invoke: list = field(default_factory=list)
    scenarios: tuple = ()
    target_language: str = ""

    def detail(self) -> dict:
        """What travels in a Candidate, in exactly the shape `Repo._locate` reads."""
        return {"root": self.root, "build": list(self.build), "invoke": list(self.invoke),
                "description": self.description, "identity": self.identity,
                "commit": self.commit, "scenarios": self.scenarios,
                "target_language": self.target_language,
                "exclude": (".git", "target", "build", "node_modules"),
                "contract": {"kind": "repo", "provenance": {
                    "subject_source": "%s@%s" % (self.identity, self.commit),
                    "contract_source": "checkout-inspection",
                    "auxiliary_generated": False,
                    "evidence": [self.root],
                }, "data": {"root": self.root, "build": self.build,
                             "target_paths": [], "verify": []}}}


class Repositories:
    """Repositories from a code-search index, cloned and made specifiable.

    Composed with `GitHub` rather than replacing it, for the same reason `PythonFunctions` is
    composed with `PyPI`: the enumerable thing is still the search, and this widens each result into
    something a scale can use. `total()` is therefore the search's total, which is the honest
    denominator because the unit of supply really is the repository.
    """

    name = "repositories"

    def __init__(self, index, *, workspace: str = "", target_language: str = "",
                 scale: str = "repo") -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "checkouts")
        # Empty means "make a same-language task". Set, it names what the solver must reimplement in,
        # and the emitted task's name says so -- see `Repo._task_name`.
        self._target_language = target_language
        self._scale = scale

    def total(self) -> int | None:
        return self._index.total()

    def page(self, number: int, *, size: int = 20):
        """One page of repositories, cloned and inspected.

        A repository that will not clone, will not build, or offers nothing to invoke contributes
        NOTHING and is not an error: that is the ordinary shape of this supply. What would be an
        error is an empty page caused by the search misbehaving, and that is the index's business --
        it raises, and the raise travels.
        """
        found = []
        for candidate in self._index.page(number, size=size):
            try:
                checkout = self.prepare(candidate)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if checkout is not None:
                found.append(to_candidate(checkout, scale=self._scale))
        return found

    def prepare(self, candidate: Candidate) -> Checkout | None:
        """Clone one repository and work out how to build and drive it. -> None if it cannot be."""
        detail = candidate.detail or {}
        url = str(detail.get("repository") or "")
        commit = str(detail.get("commit") or "")
        if not url or not commit:
            return None

        root = clone(url, commit, self._workspace)
        if root is None:
            return None
        stem = str(detail.get("identity") or "").rsplit("/", 1)[-1] or None
        language, build, produced = builder_for(root, stem or "")
        if language is None:
            return None
        return Checkout(
            identity=str(detail.get("identity") or candidate.identity),
            root=root, language=language, commit=commit,
            description=str(detail.get("description") or ""),
            build=[list(c) for c in build],
            invoke=[os.path.join("{ROOT}", produced)] if produced else [],
            target_language=self._target_language)


def clone(url: str, commit: str, workspace: str) -> str | None:
    """A shallow clone at one commit. -> where it landed, or None.

    PINNED, ALWAYS. A clone of a branch is a clone of whatever the branch pointed at that afternoon,
    and an expectation frozen against it describes a program that no longer exists. The commit comes
    from the index, which resolved it when the candidate was produced.
    """
    if not commit or not url:
        # An empty commit would make the room name empty and the fetch meaningless -- and the clone
        # would then be of whatever the default branch points at, which is the moving target this
        # function exists to avoid. Refused rather than defaulted.
        return None
    room = os.path.join(workspace, commit[:16])
    if os.path.isdir(os.path.join(room, ".git")):
        return room
    os.makedirs(room, exist_ok=True)
    try:
        for argv in (["git", "init", "--quiet"],
                     ["git", "remote", "add", "origin", url],
                     ["git", "fetch", "--quiet", "--depth", "1", "origin", commit],
                     ["git", "checkout", "--quiet", "FETCH_HEAD"]):
            done = subprocess.run(argv, cwd=room, capture_output=True, text=True,
                                  timeout=CLONE_TIMEOUT)
            if done.returncode != 0:
                shutil.rmtree(room, ignore_errors=True)
                return None
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(room, ignore_errors=True)
        return None
    return room


def builder_for(root: str, name: str = "") -> tuple:
    """What is on disk -> (language, build commands, where the product lands).

    -> (None, (), "") when no marker is present, which means this factory does not know how to build
    the repository. Skipping is the honest response: guessing a build produces a failure at the most
    expensive stage, attributed to the material, for something we never actually knew.

    `name` is the REPOSITORY's name and defaults to the checkout directory's, which is only right
    when the two agree. They do not here: a checkout is named after its commit so that two revisions
    of one repository do not collide, so without this the build would be told to produce a binary
    called `d6550df7ed8dc96f` -- which works, and then every scenario invokes a program named after
    a hash, in a task a person has to read.
    """
    name = name or os.path.basename(os.path.abspath(root))
    for marker, language, commands, product in BUILDERS:
        if not os.path.exists(os.path.join(root, marker)):
            continue
        build = [[part.format(name=name) for part in argv] for argv in commands]
        return language, build, product.format(name=name)
    return None, (), ""


def to_candidate(checkout: Checkout, *, scale: str = "repo",
                 source: str = "repositories") -> Candidate:
    """One prepared checkout -> a Candidate the repo scale can specify."""
    return Candidate(
        identity="%s@%s" % (checkout.identity, checkout.commit[:12]),
        scale=scale, language=checkout.language, source=source,
        detail=checkout.detail())


def stage_inputs(root: str, relatives, destination: str, name: str = "inputs.tar.gz") -> str:
    """Pack the files a scenario needs into a fixture tarball. -> the fixture's filename.

    THE FILES HAVE TO TRAVEL WITH THE SCENARIO, and discovering that cost a batch. Scenarios run in
    a FRESH EMPTY WORKSPACE -- deliberately, so that one probe cannot leave state behind that
    changes the next -- so an invocation naming `README.md` ran against a directory that did not
    contain it. Every file probe collapsed to the same "no such file" on stderr, which meant a
    submission that did nothing at all reproduced the whole corpus: the floor was 100%, and the task
    was refused for grading constants. Correctly, and for a reason that was entirely ours.

    A FIXTURE RATHER THAN SETUP STEPS, which is the same rule the module docstring gives. Copying
    the files in with graded `cp` commands would put steps in the corpus that never invoke the
    program, and those steps grade the host's shell -- exactly what E5 exists to catch.
    """
    import tarfile

    os.makedirs(destination, exist_ok=True)
    path = os.path.join(destination, name)
    with tarfile.open(path, "w:gz") as archive:
        for relative in relatives:
            full = os.path.join(root, relative)
            if os.path.isfile(full):
                archive.add(full, arcname=relative)
    return name


def lift(program: list, invocations, *, fixture: str | None = None):
    """Invocations of the program -> scenarios, with everything else left out.

    THE ONE RULE THIS FUNCTION EXISTS FOR. Only steps that invoke the program are graded. A lifted
    test script is mostly shell -- mkdir, echo, export -- and every one of those steps, graded,
    records what `/bin/sh` did on the machine that froze it. A corpus built from them measures the
    host's shell, passes every other check in the battery, and is precisely what E5 was written to
    catch. Rather than build such a corpus and rely on the check to reject it, the preparation goes
    into the fixture and never becomes a graded point.

    `program` is substituted at run time so the same scenario can drive the reference and a
    candidate without either being named in the corpus.
    """
    from ..observe.process.runner import Scenario, Step

    scenarios = []
    for index, argv in enumerate(invocations):
        if not argv:
            continue
        scenarios.append(Scenario(probe_id="scenario-%04d" % index,
                                  steps=[Step(argv=["{PROGRAM}"] + list(argv))],
                                  fixture=fixture))
    return tuple(scenarios)


# How many of a repository's own files are fed to the program. A corpus of flags alone is thin --
# see `probe_invocations` -- and a corpus of a thousand files is a benchmark rather than a task.
MAX_INPUT_FILES = 40

# What a program under test is plausibly given. These are the file types whose handling IS the
# behaviour, for the kind of repository this factory can build: a formatter, a parser, a linter, a
# converter. A repository holding none of them contributes flags only, and its corpus will usually
# be too thin to ship -- which is the honest outcome rather than a gap to paper over.
INPUT_SUFFIXES = (".sh", ".bash", ".json", ".yaml", ".yml", ".toml", ".md", ".c", ".h", ".go",
                  ".py", ".js", ".ts", ".rs", ".txt", ".csv", ".xml", ".html", ".ini", ".cfg")


def probe_invocations(root: str, invoke: list) -> tuple:
    """Find argument lists the program actually accepts, by running it.

    ASKED, NOT ASSUMED. Which arguments a program takes is not something to guess from a README: the
    program itself is the authority, and it is right here. Each candidate invocation is run and kept
    only if the program did something other than fail to start -- an exit code of any value is a
    behaviour worth grading, but a program that could not be executed at all is not.

    FLAGS ALONE ARE NOT A CORPUS, and finding that out cost a repo-scale refusal. `--help`,
    `--version` and a bad flag come to five invocations; three are held out for timing, and the two
    graded points that remain cannot distinguish anything, so the task is refused as too thin --
    correctly, for a repository whose actual behaviour was never exercised. What a program of this
    kind DOES is process input, so it is given the repository's own files: real data, already
    present, in the formats the program was written for. A file it rejects is kept as well, because
    how a program refuses is part of its behaviour and a corpus of only valid input grades half of
    it.
    """
    accepted = []
    for argv in (["--help"], ["--version"], [], ["--nonexistent-flag-frf"]):
        if _runs(root, invoke, argv):
            accepted.append(argv)
    for relative in _input_files(root):
        if _runs(root, invoke, [relative]):
            accepted.append([relative])
    return tuple(accepted)


def _runs(root: str, invoke: list, argv: list) -> bool:
    """Whether the program could be executed at all with these arguments."""
    try:
        done = subprocess.run(list(invoke) + list(argv), cwd=root, capture_output=True,
                              text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    # 127 is "could not execute", which is a broken build rather than a behaviour. Every other exit
    # code is something the program decided, and deciding to reject its input is a decision worth
    # grading.
    return done.returncode != 127


def _input_files(root: str) -> list:
    """Files from the repository worth feeding to its own program, nearest the root first.

    The repository's own content rather than anything synthesised: it is real, it is already there,
    and it is what the program's authors had in mind. Sorted by depth so that a corpus drawn from a
    large project is not made entirely of fixtures buried in one test directory.
    """
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target", "build")]
        for name in files:
            if name.endswith(INPUT_SUFFIXES):
                path = os.path.join(directory, name)
                try:
                    if 0 < os.path.getsize(path) <= 262144:
                        found.append(os.path.relpath(path, root))
                except OSError:
                    continue
    found.sort(key=lambda p: (p.count(os.sep), p))
    return found[:MAX_INPUT_FILES]
