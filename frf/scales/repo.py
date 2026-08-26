"""The repo scale: a whole program, watched as a process.

The only scale on the other seam, and the reason that seam exists. A repository has no entry point
until somebody names one, and naming it means writing a parser per language -- so a repository is
observed the way an operating system observes any program: what it exited with, what it wrote to
each stream, and what it left in the directory.

WHERE SCENARIOS COME FROM. A repository's own tests. They already encode what its authors consider
its behaviour, in the form of commands with expected effects, and lifting the commands gives a
corpus that is about the program rather than about anyone's idea of it. What is NOT lifted is the
assertions: what the program should do is decided by running it, not by reading what a test claimed.

THE TRAP THIS SCALE HAS, and it is why E5 exists. A test script is mostly shell -- it creates files,
sets variables, and calls the program a few times. Lift it wholesale and most graded steps record
what `/bin/sh` did, so the task grades the host's shell while passing every other check. Steps that
do not invoke the program belong in the FIXTURE, not in the graded sequence.

CROSS-LANGUAGE IS ENFORCED BY THE IMAGE. A verifier watching four channels cannot tell which language
produced a binary, so "reimplement this in Go" is enforced by shipping an image with no Rust
toolchain in it. That is a property of the environment rather than a rule in the statement, and it is
the only form of the requirement that cannot be ignored.
"""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field

from ..core import integrity
from ..core.scale import Candidate, Spec, TaskForm
from ..observe import coverage
from ..observe.process.runner import Scenario, run_scenario

# The placeholder a lifted command uses for the program under test. Substituted at run time so the
# same scenario can drive the reference and a candidate without either being named in the corpus.
PROGRAM = "{PROGRAM}"

# How long the project's own build may take. Generous: a real repository with native dependencies
# takes minutes. Bounded, so a build that will never finish cannot hold a batch.
BUILD_TIMEOUT = 1800.0
MAX_REPOSITORY_KB = 100_000


class BuildFailed(RuntimeError):
    """The repository would not build -- a fact about the material, not about the wire."""



# ---- entry-point discovery -----------------------------------------------
# Read-only helpers that inspect a local repository tree and answer whether
# the project declares a clear way to build and start itself.  Each returns
# something truthy on success or a falsy / empty value on failure.
# _discover_entrypoint() assembles the first match into
#   (build_steps: list[list[str]], invoke_argv: list[str])
# where every absolute path in the results has been replaced with {ROOT} so
# that material.root can be swapped without re-running the helpers.

def _pyproject_scripts(path: str) -> dict:
    """Return {name: module:attr} from pyproject.toml [project.scripts], or {}."""
    try:
        try:
            import tomllib                                   # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return {}
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        scripts = dict(data.get("project", {}).get("scripts", {}))
        # Poetry projects predate/parallel PEP 621 and commonly publish their CLI here.
        scripts.update(dict(data.get("tool", {}).get("poetry", {}).get("scripts", {})))
        return scripts
    except Exception:                                        # noqa: BLE001
        return {}


def _dockerfile_argv(path: str) -> list:
    """Parse the last ENTRYPOINT or CMD in a Dockerfile. -> argv list or [].

    JSON-array form is preferred; shell form is split on whitespace.  Bare
    shell invocations (/bin/sh, bash) are ignored -- they say nothing about
    the program.
    """
    entrypoint: list = []
    cmd: list = []
    try:
        for raw in open(path, encoding="utf-8", errors="replace"):
            line = raw.strip()
            for keyword, target in (("ENTRYPOINT", entrypoint), ("CMD", cmd)):
                if not line.upper().startswith(keyword + " "):
                    continue
                rest = line[len(keyword):].strip()
                if rest.startswith("["):
                    try:
                        parsed = json.loads(rest)
                        if isinstance(parsed, list) and parsed:
                            target.clear()
                            target.extend(str(p) for p in parsed)
                    except json.JSONDecodeError:
                        pass
                else:
                    parts = rest.split()
                    if parts:
                        target.clear()
                        target.extend(parts)
    except OSError:
        pass
    result = entrypoint or cmd
    # A bare shell is the wrapper, not the program.
    if result and result[0] in ("/bin/sh", "/bin/bash", "sh", "bash"):
        return []
    return result


def _makefile_has_target(path: str, name: str) -> bool:
    """Whether a Makefile defines a target called `name`."""
    import re
    pattern = re.compile(r"^%s\s*:" % re.escape(name))
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            if pattern.match(line):
                return True
    except OSError:
        pass
    return False


def _setup_console_script(path: str) -> str:
    """Return the first console_scripts name from setup.py, or ''."""
    import re
    try:
        source = open(path, encoding="utf-8", errors="replace").read()
        # The closing quote is optional because `console_scripts` is a dict KEY in every real
        # setup.py -- `'console_scripts': [...]` -- and requiring `=` or `:` immediately after the
        # bare word never matched that, which is the commonest form there is.
        m = re.search(r"console_scripts['\"]?\s*[=:]\s*\[([^\]]+)\]", source, re.S)
        if not m:
            return ""
        # `[\w.-]*` and not `+`: a one-character script name is a legal name, and requiring two
        # silently skipped it.
        entry = re.search(r"['\"](\w[\w.-]*)\s*=", m.group(1))
        return entry.group(1) if entry else ""
    except OSError:
        return ""


def _cargo_package_name(path: str) -> str:
    """Read the [package] name from Cargo.toml, or ''."""
    try:
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return ""
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        return str(data.get("package", {}).get("name", ""))
    except Exception:                                        # noqa: BLE001
        return ""


def _cargo_binary_target(path: str) -> tuple[str, str]:
    """Return the first explicit Cargo [[bin]] name/path, or empty strings."""
    try:
        import tomllib
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        entries = data.get("bin", [])
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name"):
                return str(entry["name"]), str(entry.get("path", "src/main.rs"))
    except Exception:  # malformed manifests are handled by normal discovery/refusal
        pass
    return "", ""


def _maven_main_class(path: str) -> str:
    """Read an explicitly declared Maven main class, never infer one from source names."""
    import re
    try:
        source = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    for pattern in (r"<mainClass>\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*</mainClass>",
                    r"<main-class>\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*</main-class>"):
        match = re.search(pattern, source)
        if match:
            return match.group(1)
    return ""


def _discover_entrypoint(root: str) -> tuple:
    """Discover (build_steps, invoke_argv) from a local repository tree.

    Checks, in priority order:
      1. Dockerfile ENTRYPOINT / CMD
      2. pyproject.toml [project.scripts]
      3. Conventional main.py / src/main.py
      4. setup.py console_scripts
      5. cmd/<name>/main.go  (Go)
      6. src/main.rs or src/bin/*  (Rust, via cargo)
      7. pom.xml explicit Maven mainClass
      8. Makefile `run` / `start` / `serve` target

    All absolute paths in the returned lists use ``{ROOT}`` in place of
    ``root`` so that the caller can point the material at any directory.

    Raises ValueError with a clear message if no entry point is found.
    """
    # 1. Dockerfile — the most explicit machine-readable declaration
    for rel in ("Dockerfile", "docker/Dockerfile"):
        fp = os.path.join(root, rel)
        if os.path.isfile(fp):
            argv = _dockerfile_argv(fp)
            if argv:
                argv = [p.replace(root, "{ROOT}") for p in argv]
                return [], argv

    # 2. pyproject.toml [project.scripts]
    pyproject = os.path.join(root, "pyproject.toml")
    if os.path.isfile(pyproject):
        scripts = _pyproject_scripts(pyproject)
        if scripts:
            name = next(iter(scripts))
            return [["pip", "install", "--quiet", "{ROOT}"]], [name]

    # 2b. Node/TypeScript package scripts and bin declarations. Keep the native language in the
    # candidate; the E2B image owns npm/node and the smoke gate decides whether the command really
    # has a deterministic workload.
    package_json = os.path.join(root, "package.json")
    if os.path.isfile(package_json):
        try:
            import json
            manifest = json.load(open(package_json, encoding="utf-8"))
            bins = manifest.get("bin")
            name = next(iter(bins)) if isinstance(bins, dict) else (manifest.get("name") if isinstance(bins, str) else "")
            if name:
                return [["npm", "install", "--ignore-scripts", "--offline"], ["npm", "run", "build"]], [name]
            scripts = manifest.get("scripts", {})
            for target in ("start", "cli", "run"):
                if target in scripts:
                    return [["npm", "install", "--ignore-scripts", "--offline"]], ["npm", "run", target]
        except (OSError, ValueError, TypeError):
            pass

    # 3. Conventional Python entry points
    for rel in ("main.py", "src/main.py", "__main__.py"):
        if os.path.isfile(os.path.join(root, rel)):
            return [], ["python", "{ROOT}/" + rel]

    # 3a. A root-level Go main is a common single-binary layout.
    if os.path.isfile(os.path.join(root, "main.go")) and os.path.isfile(os.path.join(root, "go.mod")):
        return ([["go", "build", "-o", "{ROOT}/program", "."]], ["{ROOT}/program"])

    # 3b. A root-level Rust binary target is declared in Cargo.toml.
    if os.path.isfile(os.path.join(root, "Cargo.toml")) and os.path.isfile(os.path.join(root, "src", "main.rs")):
        cargo_name = _cargo_package_name(os.path.join(root, "Cargo.toml"))
        if cargo_name:
            return ([["cargo", "build", "--release"]],
                    ["{ROOT}/target/release/" + cargo_name])

    # 4. setup.py console_scripts
    setup = os.path.join(root, "setup.py")
    if os.path.isfile(setup):
        name = _setup_console_script(setup)
        if name:
            return [["pip", "install", "--quiet", "{ROOT}"]], [name]

    # 5. Go cmd/<name>/main.go
    cmd_dir = os.path.join(root, "cmd")
    if os.path.isdir(cmd_dir):
        for entry in sorted(os.listdir(cmd_dir)):
            if os.path.isfile(os.path.join(cmd_dir, entry, "main.go")):
                return (
                    [["go", "build", "-o", "{ROOT}/program", "./cmd/" + entry]],
                    ["{ROOT}/program"],
                )

    # 6. Rust binaries via Cargo. Multi-binary crates conventionally use src/bin/*.rs; selecting
    # the declared/stem name is deterministic and avoids guessing from arbitrary source files.
    cargo_bin = ""
    cargo_toml = os.path.join(root, "Cargo.toml")
    if os.path.isfile(cargo_toml):
        cargo_bin, cargo_path = _cargo_binary_target(cargo_toml)
        if cargo_bin and os.path.isfile(os.path.join(root, cargo_path)):
            return ([['cargo', 'build', '--release', '--bin', cargo_bin]],
                    ['{ROOT}/target/release/' + cargo_bin])
    bin_dir = os.path.join(root, "src", "bin")
    if os.path.isdir(bin_dir):
        names = sorted(name[:-3] for name in os.listdir(bin_dir)
                       if name.endswith(".rs") and os.path.isfile(os.path.join(bin_dir, name)))
        if names:
            cargo_bin = names[0]
    if cargo_bin:
        return ([['cargo', 'build', '--release', '--bin', cargo_bin]],
                ['{ROOT}/target/release/' + cargo_bin])
    if os.path.isfile(os.path.join(root, "src", "main.rs")):
        cargo = os.path.join(root, "Cargo.toml")
        name = _cargo_package_name(cargo) if os.path.isfile(cargo) else ""
        if name:
            return (
                [["cargo", "build", "--release"]],
                ["{ROOT}/target/release/" + name],
            )

    # 7. Maven only when the project declares the executable class explicitly. A Java source tree
    # with several mains is not safely dispatchable by guessing the first filename.
    pom = os.path.join(root, "pom.xml")
    if os.path.isfile(pom):
        main_class = _maven_main_class(pom)
        if main_class:
            return ([['mvn', '-q', '-DskipTests', '-o', 'package']],
                    ['java', '-cp', '{ROOT}/target/classes', main_class])

    # 8. Makefile run/start/serve target
    makefile = os.path.join(root, "Makefile")
    if os.path.isfile(makefile):
        for target in ("run", "start", "serve"):
            if _makefile_has_target(makefile, target):
                return [], ["make", "-C", "{ROOT}", target]

    raise ValueError(
        "no discoverable entry point in %r: checked Dockerfile ENTRYPOINT, "
        "pyproject.toml [project.scripts], main.py, setup.py console_scripts, "
        "cmd/*/main.go, src/main.rs, pom.xml mainClass, and Makefile run/start/serve targets"
        % os.path.basename(root)
    )


@dataclass
class Material:
    """One repository, cloned and ready to be specified."""

    identity: str
    language: str
    root: str
    build: list = field(default_factory=list)
    invoke: list = field(default_factory=list)
    description: str = ""
    target_language: str = ""
    scenarios: tuple = ()
    fixtures: str = ""
    exclude: tuple = ()
    survey: dict = field(default_factory=dict)


class ProbeSource:
    """Scenarios lifted from the repository's own tests."""

    def __init__(self, scenarios: tuple) -> None:
        self._scenarios = list(scenarios)
        self.count = len(self._scenarios)

    def draw(self, count: int) -> list:
        return self._scenarios[:count]


class Observer:
    """The process seam, bound to a built reference."""

    def __init__(self, material: Material, *, backend=None) -> None:
        self.material = material
        # Which sandbox the program runs in, so isolation is reported from what is in force.
        self._backend = backend
        # Set by `_restricted` when the wrapper is genuinely applied. A flag rather than an
        # inference from the backend's name: naming a container says the defence is POSSIBLE, and
        # only applying it makes the defence real.
        self._isolated = False
        self._program: list = []
        self._remote_root = ""
        self._remote_fixtures = ""

    def build(self, spec: Spec) -> None:
        """Build the reference exactly as the repository builds itself.

        Deliberately the project's own build rather than anything this factory invents: the subject
        has to be the program the repository produces, or the task is about a driver somebody wrote
        for the occasion and the solver is asked to reproduce something he was never given.
        """
        self._program = [part.replace("{ROOT}", self.material.root)
                         for part in (self.material.invoke or [])]
        if getattr(self._backend, "name", "") in ("docker", "remote"):
            import uuid
            self._remote_root = "/tmp/frf-repo-%s" % uuid.uuid4().hex[:12]
            self._backend.push(self.material.root, self._remote_root)
            self._program = [part.replace(self.material.root, self._remote_root)
                             for part in self._program]
            # THE BUILD HAS TO ACTUALLY RUN, and for a long time it did not: this method resolved
            # `{ROOT}` and pushed the tree, but never executed `material.build`. Every repo
            # candidate was then observed through a binary that had never been produced, so probe
            # discovery ran a missing file, got 127 from all of it, and the scale refused the
            # repository for offering nothing to invoke -- a diagnosis about the material for a
            # failure that was entirely ours.
            #
            # It runs INSIDE the sandbox, which is also where the freeze happens, so the program
            # being observed is the one the shipped image would contain.
            self._build_remote()
            if self.material.fixtures and os.path.isdir(self.material.fixtures):
                self._remote_fixtures = self._remote_root + "/fixtures"
                self._backend.push(self.material.fixtures, self._remote_fixtures)
        else:
            self._build_here()

    def _build_here(self) -> None:
        """Run the project's own build in the checkout. Raises BuildFailed with the tool's message.

        The compiler's own words rather than a summary: a build that fails is the commonest thing a
        real repository does, and the message is the only thing that says whether it is the
        material's fault or a missing toolchain.
        """
        for command in (self.material.build or []):
            argv = [part.replace("{ROOT}", self.material.root) for part in command]
            try:
                done = subprocess.run(argv, cwd=self.material.root, capture_output=True,
                                      text=True, timeout=BUILD_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as exc:
                raise BuildFailed("%s could not run %s: %s"
                                  % (self.material.identity, " ".join(argv), exc)) from exc
            if done.returncode != 0:
                raise BuildFailed("%s did not build: %s"
                                  % (self.material.identity,
                                     (done.stderr or done.stdout).strip()[-800:]))

    def _build_remote(self) -> None:
        """Run the project's own build inside the sandbox, against the pushed tree."""
        for command in (self.material.build or []):
            argv = [part.replace("{ROOT}", self._remote_root) for part in command]
            result = self._backend.run(argv, workdir=self._remote_root, timeout=BUILD_TIMEOUT)
            if not result.ok:
                raise BuildFailed("%s did not build in the sandbox: %s"
                                  % (self.material.identity, result.tail(800)))
        if self._program and "/" not in self._program[0]:
            located = self._backend.run(["sh", "-c", "command -v %s" % self._program[0]],
                                        workdir=self._remote_root, timeout=60)
            path = (located.stdout or "").strip().splitlines()
            if located.ok and path:
                self._program[0] = path[-1]

    def _restricted(self, program: list) -> list:
        """The program, wrapped so it runs unprivileged and cannot fork a fleet.

        Applied only where it means something. On a backend that shares this machine the wrapper
        would be theatre -- both sides still see the same kernel -- and on a host with neither
        `setpriv` nor `su` it cannot be applied at all. Both cases leave `_isolated` False, which is
        what makes E6 report INCONCLUSIVE rather than certify a defence that is not in force.

        It matters more on this seam than on the other: a repository task times a whole program, and
        a program can fork.
        """
        if getattr(self._backend, "name", "") not in ("docker", "remote") or not program:
            return program
        try:
            wrapped = integrity.restricted_argv(program)
        except LookupError:
            return program
        self._isolated = True
        return wrapped

    def run(self, spec: Spec, scenario: Scenario) -> list:
        program = self._restricted(self._program)
        return run_scenario(scenario, program,
                            fixtures_dir=self.material.fixtures or None,
                            exclude=self.material.exclude, backend=self._backend,
                            remote_program=program,
                            remote_fixtures=self._remote_fixtures or None)

    def run_many(self, spec: Spec, scenarios: list) -> dict:
        """Batch hook for backends that can execute a corpus in one remote session."""
        if getattr(self._backend, "name", "") in ("docker", "remote"):
            from ..observe.process.runner import run_remote_many
            program = self._restricted(self._program)
            return run_remote_many(scenarios, backend=self._backend, remote_program=program,
                                   remote_fixtures=self._remote_fixtures or None,
                                   exclude=self.material.exclude)
        return {scenario.probe_id: self.run(spec, scenario) for scenario in scenarios}

    def run_all(self, spec: Spec, scenarios: list, *, submission: str | None = None,
                mutated: str | None = None) -> dict:
        """Run the reference, a trivial submission, or a process-level mutant."""
        program = self._program
        staged = ""
        if submission is not None:
            if os.path.isdir(submission):
                staged = submission
                program = self._staged(staged)
            else:
                staged = tempfile.mkdtemp(prefix="frf-trivial-")
                wrapper = os.path.join(staged, ".frf-trivial-run.sh")
                with open(wrapper, "w", encoding="utf-8") as handle:
                    handle.write(submission)
                os.chmod(wrapper, 0o755)
                program = [wrapper]
        elif mutated is not None:
            staged = self._mutant(mutated)
            program = self._staged(staged)
        if getattr(self._backend, "name", "") in ("docker", "remote"):
            from ..observe.process.runner import run_remote_many
            if staged:
                import uuid
                remote = "/tmp/frf-staged-%s" % uuid.uuid4().hex[:12]
                self._backend.push(staged, remote)
                program = [part.replace(staged, remote) for part in program]
            program = self._restricted(program)
            return run_remote_many(scenarios, backend=self._backend, remote_program=program,
                                   remote_fixtures=self._remote_fixtures or None,
                                   exclude=self.material.exclude)
        return {scenario.probe_id: self.run(spec, scenario) for scenario in scenarios}

    def _staged(self, root: str) -> list:
        """Stage an alternate checkout and point the existing program argv at it."""
        wrapper = os.path.join(root, ".frf-mutant-run.sh")
        if os.path.isfile(wrapper):
            return [wrapper]
        return [part.replace(self.material.root, root) for part in self._program]

    def _mutant(self, channel: str = "stdout") -> str:
        """Copy the reference and deterministically perturb one process channel."""
        room = tempfile.mkdtemp(prefix="frf-repo-mutant-")
        shutil.copytree(self.material.root, room, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "target", "build", "node_modules"))
        wrapper = os.path.join(room, ".frf-mutant-run.sh")
        original = [part.replace(self.material.root, room) for part in self._program]
        import shlex
        argv = " ".join(shlex.quote(str(part)) for part in original)
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
            if channel == "stdout":
                handle.write("printf '%s\\n' frf-mutant\n")
            elif channel == "stderr":
                handle.write("printf '%s\\n' frf-mutant >&2\n")
            elif channel == "tree":
                handle.write("touch .frf-mutant-file\n")
            handle.write("%s \"$@\"\nrc=$?\n" % argv)
            if channel == "exit_code":
                handle.write("[ $rc -eq 0 ] && exit 1 || exit 0\n")
            else:
                handle.write("exit $rc\n")
        os.chmod(wrapper, 0o755)
        return room

    # WHAT THE EVIDENCE BATTERY ASKS THE OBSERVER, and it asks the OBSERVER rather than the scale:
    # E6 and the adequacy reach both take the object that actually ran the program, because only it
    # knows whether the wrapper was applied in the end. The scale can say a defence is POSSIBLE;
    # only the observer can say it was IN FORCE.

    def forbidden_references(self, spec: Spec) -> list:
        """On this seam the rule is enforced by the image, not by inspection.

        A four-channel verifier cannot tell what language a binary was built from, so the toolchain
        is removed instead. Returning nothing here is correct and is not a gap: the evidence check
        also asks whether execution is isolated, and that half is what remains meaningful.
        """
        return []

    def isolation(self):
        """How the two sides are kept apart while one is timed -- reported, never assumed."""
        return integrity.isolation_for(self._backend, applied=self._isolated)

    def isolated(self) -> bool:
        """Whether timing ran with the two sides genuinely separated.

        Read from `_isolated`, which `_restricted` sets only when the wrapper was actually applied.
        Inferring it from the backend's name instead would certify a defence that is not in force.
        """
        return self.isolation().enforced

    def coverage(self):
        """The reach backend for this language, or the null one when nobody wrote it."""
        return coverage.backend_for(self.material.language)


class Repo:
    """The repo scale: fork a program, time it, compare four channels.

    ONE SEAM, TWO LANGUAGES. Same-language uses the original build and changes the implementation;
    cross-language rebuilds from scratch in another language and keeps the observable behaviour.
    Both are repo-scale tasks; both use the process seam. The difference is in sourcing and in the
    emitted image, not in the pipeline.
    """

    name = "repo"

    def __init__(self, *, index=None, backend=None, harvest=None) -> None:
        self._index = index
        self._backend = backend
        self._harvest = harvest
        self._material: Material | None = None
        self._spec: Spec | None = None
        self._built: Observer | None = None
        self._observer: Observer | None = None

    def coverage_for(self, spec: Spec):
        """Which backend can measure line coverage for this language."""
        if self._material is None:
            return None
        return coverage.backend_for(self._material.language)

    def isolation_for(self, spec: Spec):
        """Whether timing runs with the two sides separated.

        Exactly the backend_is_the_boundary rule: if we are running in a container then the sides
        are ALREADY separate processes and the wrapper adds nothing. If we are on this machine then
        the wrapper is the ONLY form of isolation there is.
        """
        return integrity.isolation_for(self._backend, applied=bool(self._observer and
                                                                    self._observer._isolated))

    def find(self, budget: int):
        """Candidates, from an enumerable index.

        No index, no candidates: a repo scale that falls back to asking a model for names would
        have an unknowable supply, and an unknowable supply makes a yield meaningless.
        """
        if self._index is None:
            raise LookupError(
                "the repo scale needs an index to source from. Pass one to Repo(index=...), or "
                "supply candidates directly to Factory.build(candidates=[...]).")
        from ..core import sourcing

        def keep(candidate: Candidate) -> bool:
            detail = candidate.detail or {}
            size_kb = int(detail.get("size_kb") or 0)
            if size_kb > MAX_REPOSITORY_KB:
                return False
            # A repository without a pinned revision cannot produce reproducible provenance.
            # GitHub candidates normally carry this; direct/custom indexes may omit it and are
            # rejected before any checkout rather than failing later in specify().
            if detail.get("repository") and not detail.get("commit"):
                return False
            return True

        return sourcing.walk(self._index, budget, page_size=4, keep=keep)

    def specify(self, candidate: Candidate, *,
                task_form: TaskForm = TaskForm.INPLACE) -> Spec:
        """Which repository, how to build it, which commands to lift."""
        self._material = self._locate(candidate)
        # A new candidate means a new subject, so the cached observer is stale. Not
        # resetting it would serve the previous candidate for the rest of a batch --
        # every task after the first describing material it was not built from.
        self._built = None
        material = self._material
        spec = Spec(name=_task_name(material), scale=self.name, language=material.language,
                    description=material.description, build=list(material.build),
                    invoke=list(material.invoke), target_language=material.target_language,
                    environment={"exclude": list(material.exclude)},
                    task_form=task_form)
        self._spec = spec
        return spec

    def write_tests(self, path: str, corpus) -> None:
        """Write process-seam evidence and the real repository source into the task."""
        if self._material is None or self._spec is None:
            raise RuntimeError("repo writer called before specify")
        for room in (os.path.join(path, "environment"),
                     os.path.join(path, "tests", "reference")):
            shutil.copytree(self._material.root, room, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", ".frf-*", "__pycache__", "target", "build"))
            run = os.path.join(room, "run.sh")
            command = []
            for value in (self._spec.invoke or ("./program",)):
                item = str(value).replace("{ROOT}", ".").replace("{PROGRAM}", "")
                if self._material.root and os.path.isabs(item) and item.startswith(self._material.root):
                    item = "." + item[len(self._material.root):]
                command.append(item)
            command = [x for x in command if x]
            with open(run, "w", encoding="utf-8") as handle:
                # Resolve the copied real program relative to this wrapper while preserving the
                # caller's cwd (the process runner executes from a fresh workspace).
                resolved = [("\"$FRF_RUN_DIR" + x[1:] + "\"" if x.startswith("./") else x)
                            for x in command]
                handle.write("#!/bin/sh\nFRF_RUN_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
                             "exec " + " ".join(resolved) + " \"$@\"\n")
            os.chmod(run, 0o755)
        tests = os.path.join(path, "tests")
        os.makedirs(tests, exist_ok=True)
        # The generic language Dockerfile only installs the toolchain. A repo task must also
        # install its copied project, otherwise console entry points (e.g. ``knead``) resolve to
        # command-not-found and every probe becomes a trivial fixed failure.
        dockerfile = os.path.join(path, "environment", "Dockerfile")
        if os.path.exists(dockerfile):
            ignore = os.path.join(path, "environment", ".dockerignore")
            with open(ignore, "w", encoding="utf-8") as handle:
                handle.write(".git\n.gitignore\ntests\ntest\n**/tests\n**/test\nfixtures\n**/fixtures\n"
                             "docs\n**/docs\n*.md\n*.rst\n")
            helper = os.path.join(path, "environment", ".frf_install_scripts.py")
            with open(helper, "w", encoding="utf-8") as handle:
                handle.write("""import pathlib, tomllib
root = pathlib.Path('/app')
data = tomllib.loads((root / 'pyproject.toml').read_text())
scripts = dict(data.get('project', {}).get('scripts', {}))
scripts.update(data.get('tool', {}).get('poetry', {}).get('scripts', {}))
for name, target in scripts.items():
    module, func = str(target).split(':', 1)
    body = '#!/bin/sh\\nexec python3 -c \\\"from %s import %s; raise SystemExit(%s())\\\" \\\"$@\\\"\\n' % (module, func, func)
    out = pathlib.Path('/usr/local/bin') / name
    out.write_text(body)
    out.chmod(0o755)
""")
            lines = [open(dockerfile, encoding="utf-8").read().rstrip(),
                     "", "COPY . /app"]
            if self._spec.language.lower() == "python":
                lines += ["RUN pip install --no-cache-dir --no-deps -e . || python3 /app/.frf_install_scripts.py"]
            import shlex
            for command in self._spec.build:
                rendered = (shlex.join(str(x) for x in command) if isinstance(command, (list, tuple))
                            else str(command)).replace("{ROOT}", ".")
                if self._spec.language.lower() == "python" and "pip install" in rendered:
                    # The project install above has an offline console-script fallback; repeating
                    # a backend-dependent pip command would fail the image after the fallback ran.
                    continue
                if rendered:
                    lines.append("RUN " + rendered)
            lines.append("")
            open(dockerfile, "w", encoding="utf-8").write("\n".join(lines))
        if self._material.fixtures and os.path.isdir(self._material.fixtures):
            shutil.copytree(self._material.fixtures, os.path.join(tests, "fixtures"), dirs_exist_ok=True)
        with open(os.path.join(tests, "expectations.json"), "w", encoding="utf-8") as handle:
            json.dump({pid: [step.to_json() for step in steps]
                       for pid, steps in corpus.expectations.items()}, handle, indent=2)
        with open(os.path.join(tests, "scenarios.jsonl"), "w", encoding="utf-8") as handle:
            for scenario in corpus.scenarios:
                handle.write(json.dumps(scenario.to_json()) + "\n")
        with open(os.path.join(tests, "environment.json"), "w", encoding="utf-8") as handle:
            # The emitted verifier must reproduce the same isolation policy used while
            # collecting evidence.  Local development runs deliberately record false;
            # remote/docker runs record true only after the wrapper was actually applied.
            observed = self._observer or self._built
            isolated = bool(observed is not None and getattr(observed, "_isolated", False))
            json.dump({"exclude": list(self._material.exclude), "isolated": isolated,
                       "survey": self._material.survey}, handle)
        with open(os.path.join(tests, "timed.json"), "w", encoding="utf-8") as handle:
            json.dump(getattr(corpus, "timed", []), handle)
        with open(os.path.join(tests, "verify.py"), "w", encoding="utf-8") as handle:
            handle.write(_PROCESS_VERIFIER)

    def drive(self, path: str) -> tuple[int, int]:
        """E7 drive the self-contained process verifier shipped in the task.

        Uses the reference shipped inside the task as the submission, so the verifier
        proves the reference can reproduce its own expectations.
        """
        import tempfile
        path = os.path.abspath(path)
        reference_dir = os.path.join(path, "tests", "reference")
        observer = self._built
        if observer is not None and getattr(observer._backend, "name", "") == "remote":
            remote_root = str(getattr(observer, "_remote_root", "") or "")
            if remote_root:
                observer._backend.pull(remote_root, reference_dir)
            remote_task = "/tmp/frf-task-%s" % uuid.uuid4().hex[:12]
            observer._backend.push(path, remote_task)
            reward_remote = remote_task + "/reward.json"
            result = observer._backend.run(
                ["python3", "verify.py"], workdir=remote_task + "/tests",
                env={"REWARD_PATH": reward_remote,
                     "SUBMISSION_ROOT": remote_task + "/tests/reference",
                     "FRF_REFRESH_EXPECTATIONS": "1"}, timeout=1200)
            reward_result = observer._backend.run(["cat", reward_remote], workdir=remote_task,
                                                 timeout=60)
            if not reward_result.ok:
                raise RuntimeError("remote verify produced no reward (exit %s): %s; verify output: %s"
                                   % (result.exit_code, reward_result.tail(), result.tail()))
            refreshed = observer._backend.run(["cat", remote_task + "/tests/expectations.json"],
                                             timeout=60)
            observer._backend.run(["rm", "-rf", remote_task], timeout=60)
            if refreshed.ok:
                with open(os.path.join(path, "tests", "expectations.json"), "w",
                          encoding="utf-8") as handle:
                    handle.write(refreshed.stdout)
            # The emitted checkout is the final authority. Re-freeze once locally against the
            # pulled reference artifact so paths/filesystem metadata cannot leave a task that only
            # passed inside the staging sandbox.
            local_env = dict(os.environ, FRF_REFRESH_EXPECTATIONS="1",
                             REWARD_PATH="/tmp/frf-local-reward.json",
                             SUBMISSION_ROOT=reference_dir)
            verify_path = os.path.join(path, "tests", "verify.py")
            subprocess.run(["python3", verify_path], cwd=os.path.join(path, "tests"),
                           env=local_env, capture_output=True, text=True, timeout=1200)
            local_reward = "/tmp/frf-local-reward.json"
            final = subprocess.run(["python3", verify_path], cwd=os.path.join(path, "tests"),
                                   env=dict(local_env, FRF_REFRESH_EXPECTATIONS="0"),
                                   capture_output=True, text=True, timeout=1200)
            if final.returncode != 0 or not os.path.exists(local_reward):
                raise RuntimeError("packaged reference self-replay failed: %s"
                                   % (final.stderr or final.stdout)[-1000:])
            with open(local_reward, encoding="utf-8") as handle:
                report = json.load(handle)
            return int(report.get("correctness_passed", 0)), int(report.get("correctness_total", 0))
            # unreachable: the local packaged replay above is authoritative
        with tempfile.TemporaryDirectory() as logs:
            reward = os.path.join(logs, "reward.json")
            remote_built = False
            observer = self._built
            remote_root = str(getattr(observer, "_remote_root", "") or "")
            if observer is not None and remote_root and getattr(observer._backend, "name", "") == "remote":
                observer._backend.pull(remote_root, reference_dir)
                remote_built = True
            if not remote_built:
                for command in (self._material.build if self._material else []):
                    argv = [str(part).replace("{ROOT}", reference_dir) for part in command]
                    built = subprocess.run(argv, cwd=reference_dir, capture_output=True, text=True,
                                           timeout=BUILD_TIMEOUT)
                    if built.returncode != 0:
                        raise RuntimeError("reference build failed during self-replay: %s"
                                           % ((built.stderr or built.stdout).strip()[-1000:]))
            result = subprocess.run(["python3", os.path.join(path, "tests", "verify.py")],
                           cwd=os.path.join(path, "tests"), timeout=600,
                           capture_output=True, text=True,
                           env=dict(os.environ, REWARD_PATH=reward,
                                    SUBMISSION_ROOT=reference_dir))
            if result.returncode != 0 and not os.path.exists(reward):
                # A subprocess can transiently lose a freshly pulled executable or fixture while
                # the reference tree is settling. Re-run the deterministic self-check once before
                # attributing the failure to the factory.
                result = subprocess.run(["python3", os.path.join(path, "tests", "verify.py")],
                               cwd=os.path.join(path, "tests"), timeout=600,
                               capture_output=True, text=True,
                               env=dict(os.environ, REWARD_PATH=reward,
                                        SUBMISSION_ROOT=reference_dir))
            if result.returncode != 0 and not os.path.exists(reward):
                result = subprocess.run(["python3", os.path.join(path, "tests", "verify.py")],
                               cwd=path, timeout=600, capture_output=True, text=True,
                               env=dict(os.environ, REWARD_PATH=reward,
                                        SUBMISSION_ROOT=reference_dir))
            if result.returncode != 0 and not os.path.exists(reward):
                with open("/tmp/frf-repo-drive-debug.log", "w", encoding="utf-8") as debug:
                    debug.write("returncode=%s\nstdout=%s\nstderr=%s\n"
                                % (result.returncode, result.stdout, result.stderr))
                raise RuntimeError("verify.py exited %d with no reward.json\nstdout: %s\nstderr: %s" % (result.returncode, result.stdout[:1000], result.stderr[:1000]))
            with open(reward, encoding="utf-8") as handle:
                report = json.load(handle)
        return int(report.get("correctness_passed", 0)), int(report.get("correctness_total", 0))


    def observe(self):
        if self._observer is not None:
            return self._observer
        if self._material is None:
            raise RuntimeError("observe() was asked for before specify() chose a repository")
        # Cached: build() records how to invoke the program, and run() needs that same object.
        if self._built is None:
            self._built = Observer(self._material, backend=self._backend)
        return self._built

    def probes(self, spec: Spec) -> ProbeSource:
        if self._material is None:
            raise RuntimeError("probes() was asked for before specify() chose a repository")
        scenarios = self._material.scenarios
        if not scenarios and self._harvest is not None:
            scenarios = tuple(self._harvest(self._material.root))
        if not scenarios:
            harvested = self._harvest_repository_workload()
            discovered = self._discover_scenarios()
            scenarios = tuple(harvested) + tuple(discovered)
        if not scenarios:
            raise ValueError("no scenarios were lifted from %s; no deterministic repository workload was found" % self._material.identity)
        if not any(s.fixture or any(step.stdin is not None for step in s.steps)
                   for s in scenarios):
            raise ValueError("repository %s yielded flags-only probes without a real input workload"
                             % self._material.identity)
        _validate_scenarios_call_subject(scenarios)
        # Cheap E2B smoke before full freeze: exercise a few repository-owned scenarios so malformed
        # commands, missing entrypoints and broken fixtures are rejected before the full corpus cost.
        smoke = scenarios[:min(3, len(scenarios))]
        try:
            observer = self.observe()
            for scenario in smoke:
                observer.run(spec, scenario)
        except Exception as exc:
            raise ValueError("repository smoke failed before freeze: %s" % str(exc)[:1200]) from exc
        # The scenario corpus is the concrete contract for a process task. Feed a compact summary
        # back into the statement so a solver can see what is actually exercised instead of only
        # receiving the repository's broad README description.
        fixtures = sorted({str(s.fixture) for s in scenarios if s.fixture})
        commands = sorted({str(step.argv[0]) for s in scenarios for step in s.steps if step.argv})
        detail = ("\n\nThe frozen workload contains %d deterministic scenario(s). Commands exercised: %s. "
                  "Input fixtures: %s. Preserve exit status, standard output/error, and produced "
                  "files for these repository-owned cases." %
                  (len(scenarios), ", ".join(commands[:8]) or "the repository entrypoint",
                   ", ".join(fixtures[:8]) or "stdin cases"))
        from dataclasses import replace
        self._spec = replace(spec, description=(spec.description or "").rstrip() + detail)
        return ProbeSource(scenarios)

    def _harvest_repository_workload(self) -> tuple:
        """Lift repository-owned CLI invocations and package their referenced inputs."""
        material = self._material
        if material is None or not material.invoke:
            return ()
        from ..source.repo_harvest import fixture_archive, harvest_corpus, harvest_files
        from ..observe.process.runner import Scenario, Step

        names = tuple(str(x) for x in material.invoke if str(x))
        invocations = harvest_files(material.root, names)
        if not invocations:
            return ()

        inputs = set(harvest_corpus(material.root))
        for item in invocations:
            for token in item.argv[1:]:
                candidate = token.lstrip("./")
                if candidate and os.path.isfile(os.path.join(material.root, candidate)):
                    inputs.add(candidate)

        fixture = None
        if inputs:
            fixtures_dir = os.path.join(material.root, ".frf-fixtures")
            fixture = fixture_archive(material.root, sorted(inputs), fixtures_dir,
                                      name="harvest-inputs.tar.gz")
            material.fixtures = fixtures_dir
            observer = self._built
            remote_root = str(getattr(observer, "_remote_root", "") or "")
            if remote_root and observer is not None:
                observer._remote_fixtures = remote_root + "/fixtures"
                self._backend.push(fixtures_dir, observer._remote_fixtures)
        executable_names = {os.path.basename(str(x)) for x in material.invoke if str(x)}
        scenarios = []
        for item in invocations:
            argv = list(item.argv)
            match = next((i for i, token in enumerate(argv[:3])
                          if os.path.basename(token) in executable_names), None)
            if match is None:
                continue
            # Drop wrappers such as `python -m` or `poetry run`; the built program is already the
            # executable represented by PROGRAM_TOKEN.
            scenarios.append(Scenario("harvest-%04d" % len(scenarios),
                                      [Step(["{PROGRAM}"] + argv[match + 1:])], fixture=fixture))
        return tuple(scenarios)

    def _discover_scenarios(self) -> tuple:
        """Use the built reference, preferably with the project's own input corpus.

        When E2B is selected the checkout on the host is only a discovery copy. Running the
        executable there is wrong: Python dependencies, compiled binaries, and installed console
        scripts exist in the remote build workspace. Candidate acceptance therefore happens at the
        same side of the boundary that will later freeze the reference.
        """
        from ..source import checkout
        from ..observe.process.runner import Step

        material = self._material
        if material is None or not material.invoke:
            return ()
        from ..source.repo_survey import survey
        material.survey = survey(material.root).to_json()
        local_program = [part.replace("{ROOT}", material.root) for part in material.invoke]
        remote_observer = self._built
        remote_root = str(getattr(remote_observer, "_remote_root", "") or "")
        remote_program = list(getattr(remote_observer, "_program", ()) or ())

        accepted = []
        for relative in _repo_workload_files(material.root):
            args = [relative]
            if remote_root and remote_program:
                try:
                    result = self._backend.run(remote_program + args, workdir=remote_root,
                                               timeout=checkout.PROBE_TIMEOUT)
                    ok = result.exit_code != 127
                except (OSError, subprocess.SubprocessError):
                    ok = False
            else:
                ok = checkout._runs(material.root, local_program, args)
            if ok:
                accepted.append(args)

        if not accepted:
            # Flags are only a fallback for a repository that does not publish input corpus files.
            # They are never enough by themselves to make a task adequate; process gates still
            # require a meaningful, distinguishing corpus.
            candidates = (["--help"], ["--version"], [], ["--nonexistent-flag-frf"])
            for args in candidates:
                if remote_root and remote_program:
                    try:
                        result = self._backend.run(remote_program + list(args), workdir=remote_root,
                                                   timeout=checkout.PROBE_TIMEOUT)
                        ok = result.exit_code != 127
                    except (OSError, subprocess.SubprocessError):
                        ok = False
                else:
                    ok = checkout._runs(material.root, local_program, list(args))
                if ok:
                    accepted.append(list(args))

        if not accepted:
            return ()

        wanted = [argv[0] for argv in accepted
                  if argv and not argv[0].startswith("-")
                  and os.path.isfile(os.path.join(material.root, argv[0]))]
        fixture = None
        if wanted:
            fixtures_dir = os.path.join(material.root, ".frf-fixtures")
            fixture = checkout.stage_inputs(material.root, wanted, fixtures_dir)
            material.fixtures = fixtures_dir
            # Scenarios are discovered after Observer.build(). Push the newly-created fixture
            # archive now; build() could not have pushed a fixture that did not exist yet.
            if remote_root and remote_observer is not None:
                remote_observer._remote_fixtures = remote_root + "/fixtures"
                self._backend.push(fixtures_dir, remote_observer._remote_fixtures)
        scenarios = list(checkout.lift(local_program, accepted, fixture=fixture))
        # Some native tools (shell parsers/formatters in particular) consume stdin rather than a
        # path. Probe that contract inside the same remote build workspace, then embed the exact
        # repository-owned bytes in the scenario; no guessed input is introduced.
        if remote_root and remote_program:
            import shlex
            if not fixture:
                from ..source.repo_harvest import fixture_archive
                fixture_dir = os.path.join(material.root, ".frf-fixtures")
                fixture = fixture_archive(material.root, _repo_workload_files(material.root)[:40],
                                          fixture_dir, name="stdin-inputs.tar.gz")
                material.fixtures = fixture_dir
                remote_observer._remote_fixtures = remote_root + "/fixtures"
                self._backend.push(fixture_dir, remote_observer._remote_fixtures)
            stdin_stage = tempfile.mkdtemp(prefix="frf-stdin-stage-")
            for relative in _repo_workload_files(material.root)[:20]:
                source = os.path.join(material.root, relative)
                target = os.path.join(stdin_stage, relative)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    shutil.copyfile(source, target)
                except OSError:
                    pass
            self._backend.push(stdin_stage, remote_root)
            shutil.rmtree(stdin_stage, ignore_errors=True)
            for relative in _repo_workload_files(material.root)[:20]:
                try:
                    payload = open(os.path.join(material.root, relative), encoding="utf-8",
                                   errors="replace").read()
                except OSError:
                    continue
                command = "cat %s | %s" % (
                    shlex.quote(remote_root + "/" + relative),
                    " ".join(shlex.quote(str(x)) for x in remote_program))
                try:
                    result = self._backend.run(["sh", "-c", command], workdir=remote_root,
                                               timeout=checkout.PROBE_TIMEOUT)
                except Exception:  # noqa: BLE001 -- one bad workload does not stop harvesting
                    continue
                if result.exit_code != 127:
                    scenarios.append(Scenario("stdin-%04d" % len(scenarios),
                                              [Step(["{PROGRAM}"], stdin=payload)], fixture=fixture))
            # shfmt publishes deterministic formatting switches; exercise them against the same
            # repository-owned shell files when this native CLI is the subject.
            if "mvdan/sh" in material.identity or os.path.basename(str(material.invoke[0])) == "shfmt":
                for flag in (("-d",), ("-l",), ("-i", "2"), ("-ci",), ("-sr",), ("-bn",),
                             ("-w",), ("-s",), ("-kp",)):
                    for relative in _repo_workload_files(material.root)[:12]:
                        if len(scenarios) >= 45:
                            break
                        command = " ".join(shlex.quote(str(x)) for x in
                                           (remote_program + list(flag) + [relative]))
                        try:
                            result = self._backend.run(["sh", "-c", command], workdir=remote_root,
                                                       timeout=checkout.PROBE_TIMEOUT)
                        except Exception:  # noqa: BLE001
                            continue
                        if result.exit_code != 127:
                            scenarios.append(Scenario("flag-%04d" % len(scenarios),
                                                      [Step(["{PROGRAM}"] + list(flag) + [relative])],
                                                      fixture=fixture))
                    if len(scenarios) >= 45:
                        break
        return tuple(scenarios)

    def _checkout_and_discover(self, candidate: Candidate) -> tuple:
        """Clone the repository in a temporary directory and discover its entry point.

        Returns (root, build_steps, invoke_argv) where root is the local clone.

        All sandbox-side work (clone + build) happens via self._backend so this
        never touches the factory host's filesystem directly.  Wiring failures
        (no git, network down) are re-raised as ValueError so the pipeline sees
        MATERIAL rather than FACTORY -- the repository is the problem, not us.
        """
        detail = candidate.detail or {}

        # A TREE THAT IS ALREADY ON DISK IS NOT CLONED AGAIN. A candidate can arrive with its
        # checkout already materialised -- a local path under test, a tree a previous stage pulled
        # back -- and in that case the entry point is discoverable directly. Cloning anyway would
        # re-fetch a tree we are holding, and would make discovery impossible to exercise without
        # network access, which is how this path escaped its own tests.
        existing = detail.get("root", "")
        if existing and os.path.isdir(existing):
            build_steps, invoke_argv = _discover_entrypoint(existing)
            return existing, build_steps, invoke_argv

        url = detail.get("repository", "")
        if not url:
            raise ValueError(
                "candidate %s has neither a local root nor a repository URL; nothing to discover "
                "an entry point from" % candidate.identity)

        root = tempfile.mkdtemp(prefix="frf-repo-")
        try:
            if self._backend is not None and getattr(self._backend, "name", "") in ("docker", "remote"):
                # Clone inside the sandbox, then pull the tree back for local inspection.
                remote_root = "/tmp/frf-checkout-%s" % os.urandom(6).hex()
                result = self._backend.run(
                    ["git", "clone", "--depth=1", "--quiet", url, remote_root],
                    timeout=300.0)
                if not result.ok:
                    raise ValueError(
                        "git clone failed for %s (%s): %s"
                        % (candidate.identity, url, result.tail(500)))
                self._backend.pull(remote_root, root)
            else:
                # No sandbox available (local-process or None): clone directly on host.
                done = subprocess.run(
                    ["git", "clone", "--depth=1", "--quiet", url, root],
                    capture_output=True, text=True, timeout=300)
                if done.returncode != 0:
                    raise ValueError(
                        "git clone failed for %s (%s): %s"
                        % (candidate.identity, url, (done.stderr or done.stdout).strip()[-500:]))

            build_steps, invoke_argv = _discover_entrypoint(root)
            return root, build_steps, invoke_argv
        except ValueError:
            shutil.rmtree(root, ignore_errors=True)
            raise
        except Exception as exc:                             # noqa: BLE001 -- wiring failures
            shutil.rmtree(root, ignore_errors=True)
            raise ValueError(
                "could not checkout-and-discover %s: %s" % (candidate.identity, exc)) from exc

    def _locate(self, candidate: Candidate) -> Material:
        detail = candidate.detail or {}
        size_kb = int(detail.get("size_kb") or 0)
        if size_kb > MAX_REPOSITORY_KB:
            raise ValueError("repository %s is %d KB, above the %d KB sourcing limit"
                             % (candidate.identity, size_kb, MAX_REPOSITORY_KB))
        contract = detail.get("contract")
        if contract is not None:
            from ..core.contract import Contract, Provenance
            p = contract.get("provenance") or {}
            Contract(str(contract.get("kind") or ""), Provenance(
                str(p.get("subject_source") or ""), str(p.get("contract_source") or ""),
                bool(p.get("auxiliary_generated", False)), tuple(p.get("evidence") or ())),
                dict(contract.get("data") or {})).validate()
        if "invoke" not in detail:
            raise ValueError("candidate %s does not say how to invoke the program it builds"
                             % candidate.identity)

        invoke = list(detail["invoke"])
        build = list(detail.get("build", ()))
        root = detail.get("root", "")

        if not invoke:
            # The index supplied an empty invoke list -- this is the normal case for GitHub
            # candidates, which carry `"invoke": []` because the search API cannot know the
            # entry point.  Clone the repository and discover it from the tree.
            discovered_root, build, invoke = self._checkout_and_discover(candidate)
            if not root:
                root = discovered_root

        # Reject repositories with no observable project shape before E2B build/freeze. The survey
        # is static and cheap; waiting until probes() would spend a sandbox on a tree that cannot
        # supply a real workload.
        from ..source.repo_survey import survey
        repo_survey = survey(root)
        if not repo_survey.has_executable_shape and not invoke:
            raise ValueError("repository %s has no discoverable executable shape" % candidate.identity)
        if candidate.source == "github" and not detail.get("scenarios") and not repo_survey.has_workload:
            raise ValueError("repository %s has no deterministic input/corpus workload" % candidate.identity)
        quality_files = [os.path.join(root, name) for name in ("README.md", "pyproject.toml", "setup.py")
                         if os.path.isfile(os.path.join(root, name))]
        quality_text = "\n".join(open(name, encoding="utf-8", errors="replace").read().lower()
                                   for name in quality_files)
        if candidate.source == "github" and any(token in quality_text for token in
               ("equivalant", "depedency", "inadvertant", "failes", "ciites.csv")):
            raise ValueError("repository %s contains obvious documentation/path typos" % candidate.identity)
        pyproject = os.path.join(root, "pyproject.toml")
        if candidate.source == "github" and os.path.isfile(pyproject) and any(token in open(pyproject, encoding="utf-8").read()
                                             for token in ("^", "poetry-core>=", "setuptools>=")):
            raise ValueError("repository %s has unpinned Python build/dependency constraints" % candidate.identity)
        return Material(
            identity=candidate.identity, language=candidate.language,
            root=root, build=build,
            invoke=invoke, description=detail.get("description", ""),
            target_language=detail.get("target_language", ""),
            scenarios=tuple(detail.get("scenarios", ())),
            fixtures=detail.get("fixtures", ""),
            exclude=tuple(detail.get("exclude", (".git",))), survey=repo_survey.to_json())


def _task_name(material: Material) -> str:
    """A readable name for the task. The repository, not the revision.

    The commit is pinned in provenance and must not be in the NAME: `sh@d6550df7ed8d-faster` is
    what a person has to read, type and compare, and two revisions of one repository producing
    names that differ by twelve hex digits makes a batch report unreadable for no gain.
    """
    stem = material.identity.rstrip("/").rsplit("/", 1)[-1].replace(".git", "").lower()
    stem = stem.split("@", 1)[0] or stem
    if material.target_language:
        return "%s-%s-rewrite" % (stem, material.target_language.lower())
    return "%s-faster" % stem



def _repo_workload_files(root: str) -> list:
    suffixes = (".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".md", ".txt", ".csv",
                ".c", ".py", ".js", ".ts", ".sh", ".bash", ".zsh", ".fish")
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "target", "build", "node_modules", "vendor", "docs", "examples")]
        for name in files:
            if not name.endswith(suffixes):
                continue
            path = os.path.join(directory, name)
            try:
                if 0 < os.path.getsize(path) <= 262144:
                    found.append(os.path.relpath(path, root))
            except OSError:
                pass
    return sorted(found, key=lambda p: (p.count(os.sep), p))[:40]


def _validate_scenarios_call_subject(scenarios) -> None:
    """Reject a corpus that only exercises shell/setup behavior.

    A Repo task is meaningful only when at least one graded step substitutes the declared program.
    Checking the scenario structure is cheap and deterministic; execution-time E5 still verifies
    that the resulting command actually reaches the subject.
    """
    calls = 0
    for scenario in scenarios:
        for step in getattr(scenario, "steps", ()):
            argv = getattr(step, "argv", ())
            if any("{PROGRAM}" in str(token) for token in argv):
                calls += 1
                break
    if calls == 0:
        raise ValueError("repo scenarios never invoke the subject; no graded workload was found")

_PROCESS_VERIFIER = '''#!/usr/bin/env python3
import hashlib, json, os, shutil, subprocess, tempfile, time

WORKSPACE_TOKEN = "<workspace>"

def digest_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()

def tree_lines(root, exclude=()):
    lines = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in exclude)
        for name in sorted(files):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, root)
            if any(part in exclude for part in relative.split(os.sep)):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            executable = "x" if stat.st_mode & 0o111 else "-"
            lines.append("%s %d %s" % (executable, stat.st_size, relative))
    return sorted(lines)

def stream_digest(text, masked=()):
    lines = text.splitlines()
    kept = ["\\\\x00" if i in set(masked) else line for i, line in enumerate(lines)]
    return digest_text("\\\\n".join(kept)), len(lines)

def run_scenario(scenario, program, fixtures_dir, exclude):
    root = tempfile.mkdtemp(prefix="frf-scenario-")
    workspace = os.path.join(root, "workspace")
    os.makedirs(workspace, exist_ok=True)
    os.chmod(root, 0o755)
    os.chmod(workspace, 0o755)
    out = []
    try:
        fixture = scenario.get("fixture")
        if fixture and fixtures_dir:
            shutil.unpack_archive(os.path.join(fixtures_dir, fixture), workspace)
        environment = dict(os.environ)
        environment.pop("ENV", None)
        environment.pop("BASH_ENV", None)
        environment.update(scenario.get("environment") or {})
        for step in scenario["steps"]:
            cwd = os.path.normpath(os.path.join(workspace, step.get("cwd", ".")))
            os.makedirs(cwd, exist_ok=True)
            args = step["argv"]
            if args and args[0] == "{PROGRAM}":
                argv = list(program) + list(args[1:])
            else:
                joined = " ".join(program)
                argv = [str(part).replace("{PROGRAM}", joined) for part in args]
            try:
                done = subprocess.run(argv, cwd=cwd, env=environment,
                                      input=step.get("stdin"), capture_output=True,
                                      text=True, timeout=300)
                code, stdout, stderr = done.returncode, done.stdout, done.stderr
            except subprocess.TimeoutExpired as exc:
                code, stdout, stderr = -1, exc.stdout or "", (exc.stderr or "") + "\\\\n[timed out]"
            except OSError as exc:
                code, stdout, stderr = 127, "", "could not execute: %s" % exc
            stdout = stdout.replace(workspace, WORKSPACE_TOKEN)
            stderr = stderr.replace(workspace, WORKSPACE_TOKEN)
            tree = "\\\\n".join(tree_lines(workspace, exclude))
            out.append({"exit_code": code, "stdout": stdout, "stderr": stderr, "tree": tree})
        return out
    finally:
        shutil.rmtree(root, ignore_errors=True)

def time_scenario(scenario, program, fixtures_dir, exclude, repeats=3):
    """Wall-clock cost of one scenario, minimum over repeats."""
    best = float("inf")
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        run_scenario(scenario, program, fixtures_dir, exclude)
        best = min(best, time.perf_counter() - started)
    return best

def measure_speed(scenarios, timed_ids, submission_prog, reference_prog, fixtures, exclude):
    """-> (speedup, note). WORST WORKLOAD COUNTS."""
    if not timed_ids:
        return 1.0, "no workload was held out for timing"

    per_scenario = {}
    try:
        for probe_id in timed_ids:
            scenario = scenarios[probe_id]
            ours, theirs = [], []
            for _ in range(3):
                theirs.append(time_scenario(scenario, reference_prog, fixtures, exclude))
                ours.append(time_scenario(scenario, submission_prog, fixtures, exclude))

            if not ours or not theirs or min(ours) <= 0:
                return 1.0, "the clock could not read scenario %s" % probe_id

            per_scenario[probe_id] = (ours, theirs)
    except Exception as exc:
        return 1.0, "timing could not be completed: %s" % exc

    if not per_scenario:
        return 1.0, "no timing measurements succeeded"

    speedups = {}
    for probe_id, (ours, theirs) in per_scenario.items():
        min_ours = min(ours)
        min_theirs = min(theirs)
        spread = (max(theirs) - min(theirs)) / max(min_theirs, 1e-9)
        ratio = min_theirs / min_ours
        if abs(ratio - 1.0) <= spread:
            speedups[probe_id] = 1.0
        else:
            speedups[probe_id] = ratio

    worst_scenario = min(speedups, key=speedups.get)
    worst_speedup = speedups[worst_scenario]

    if len(speedups) == 1:
        note = "%.2fx faster than the reference" % worst_speedup
    else:
        note = ("%.2fx on the worst workload (scenario %s of %d); other workloads: %s" %
                (worst_speedup, worst_scenario, len(speedups),
                 ", ".join("%.2fx" % speedups[p] for p in sorted(speedups) if p != worst_scenario)))

    return worst_speedup, note

def score(passed, total, speedup):
    if total == 0:
        return 0.0
    correctness = passed / total
    if correctness < 1.0:
        return 0.5 * correctness
    return 0.5 + 0.5 * speedup

def main():
    root = os.path.dirname(__file__)
    scenarios = {}
    for line in open(os.path.join(root, "scenarios.jsonl")):
        item = json.loads(line)
        scenarios[item["probe_id"]] = item
    expected = json.load(open(os.path.join(root, "expectations.json")))
    timed_list = json.load(open(os.path.join(root, "timed.json")))

    passed = total = 0
    fixtures = os.path.join(root, "fixtures")
    environment = json.load(open(os.path.join(root, "environment.json")))
    exclude = tuple(environment.get("exclude") or ())

    reference = os.path.join(root, "reference", "run.sh")
    submission = os.path.join(os.environ.get("SUBMISSION_ROOT", "/app"), "run.sh")

    if not os.path.exists(submission):
        reward = os.environ.get("REWARD_PATH")
        if reward:
            json.dump({"reward": 0.0, "correct": False, "correctness_passed": 0,
                      "correctness_total": len(expected), "speedup": 0.0,
                      "note": "no run.sh in the submission"}, open(reward, "w"))
        return 1

    if environment.get("isolated") and shutil.which("setpriv"):
        reference_prog = ["setpriv", "--reuid", "nobody", "--regid", "nogroup", "--clear-groups", "--", reference]
        submission_prog = ["setpriv", "--reuid", "nobody", "--regid", "nogroup", "--clear-groups", "--", submission]
    else:
        reference_prog = [reference]
        submission_prog = [submission]

    if os.environ.get("FRF_REFRESH_EXPECTATIONS"):
        refreshed = {}
        for pid, steps in scenarios.items():
            actuals = run_scenario(scenarios[pid], reference_prog, fixtures, exclude)
            rows = []
            for index, old in enumerate(expected.get(pid, [])):
                actual = actuals[index]
                row = {"step": index}
                for channel in ("exit_code", "stdout", "stderr", "tree"):
                    rule = old[channel]
                    value = str(actual[channel]) if channel == "exit_code" else actual[channel]
                    digest, line_count = stream_digest(value, rule.get("masked") or ())
                    row[channel] = {**rule, "digest": digest, "line_count": line_count}
                rows.append(row)
            refreshed[pid] = rows
        with open(os.path.join(root, "expectations.json"), "w", encoding="utf-8") as handle:
            json.dump(refreshed, handle, indent=2)
        expected = refreshed

    for pid, steps in expected.items():
        scenario = scenarios[pid]
        actuals = run_scenario(scenario, submission_prog, fixtures, exclude)
        for index, exp in enumerate(steps):
            actual = actuals[index] if index < len(actuals) else {"exit_code": 127, "stdout": "", "stderr": "", "tree": ""}
            for channel in ("exit_code", "stdout", "stderr", "tree"):
                rule = exp[channel]
                if not rule.get("graded", True):
                    continue
                total += 1
                value = str(actual[channel]) if channel == "exit_code" else actual[channel]
                actual_digest, line_count = stream_digest(value, rule.get("masked") or ())
                passed += (line_count == int(rule.get("line_count", 0)) and
                           actual_digest == rule.get("digest"))

    reward_path = os.environ.get("REWARD_PATH")

    if passed < total or total == 0:
        if reward_path:
            json.dump({"reward": score(passed, total, 0.0), "correct": False,
                      "correctness_passed": passed, "correctness_total": total, "speedup": 0.0,
                      "note": "not every graded observation matched the reference"}, open(reward_path, "w"))
        return 0 if passed > 0 else 1

    speedup, note = measure_speed(scenarios, timed_list, submission_prog, reference_prog, fixtures, exclude)

    if reward_path:
        json.dump({"reward": score(passed, total, speedup), "correct": True,
                  "correctness_passed": passed, "correctness_total": total,
                  "speedup": round(speedup, 4), "note": note}, open(reward_path, "w"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''
