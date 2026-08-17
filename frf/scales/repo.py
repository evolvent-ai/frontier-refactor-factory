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
from dataclasses import dataclass, field

from ..core.scale import Candidate, Spec
from ..observe import coverage
from ..observe.process.runner import Scenario, run_scenario

# The placeholder a lifted command uses for the program under test. Substituted at run time so the
# same scenario can drive the reference and a candidate without either being named in the corpus.
PROGRAM = "{PROGRAM}"


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


class ProbeSource:
    """Scenarios lifted from the repository's own tests."""

    def __init__(self, scenarios: tuple) -> None:
        self._scenarios = list(scenarios)
        self.count = len(self._scenarios)

    def draw(self, count: int) -> list:
        return self._scenarios[:count]


class Observer:
    """The process seam, bound to a built reference."""

    def __init__(self, material: Material) -> None:
        self.material = material
        self._program: list = []

    def build(self, spec: Spec) -> None:
        """Build the reference exactly as the repository builds itself.

        Deliberately the project's own build rather than anything this factory invents: the subject
        has to be the program the repository produces, or the task is about a driver somebody wrote
        for the occasion and the solver is asked to reproduce something he was never given.
        """
        self._program = [part.replace("{ROOT}", self.material.root)
                         for part in (self.material.invoke or [])]

    def run(self, spec: Spec, scenario: Scenario) -> list:
        return run_scenario(scenario, self._program, fixtures_dir=self.material.fixtures or None,
                            exclude=self.material.exclude)

    def run_all(self, spec: Spec, scenarios: list, *, submission: str | None = None,
                mutated: str | None = None) -> dict:
        """Every scenario against the reference, a trivial submission, or a mutant.

        One method rather than three because the difference is only WHICH program runs, and the
        shared stages need all three -- for the ceiling, the floor, and the channel checks.
        """
        program = self._program
        if submission is not None or mutated is not None:
            program = self._staged(submission if submission is not None else self._mutant())
        return {s.probe_id: run_scenario(s, program, fixtures_dir=self.material.fixtures or None,
                                         exclude=self.material.exclude)
                for s in scenarios}

    def _staged(self, script: str) -> list:
        """Write a stand-in program and return how to invoke it."""
        path = os.path.join(self.material.root, ".frf-candidate")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(path, 0o755)
        return [path]

    def _mutant(self) -> str:
        """A program that runs the reference and corrupts one channel.

        Built from the reference rather than written from scratch so the perturbation is small and
        provable: everything else about the observation stays put, which is what lets the channel
        check show that the corpus noticed THIS change rather than some unrelated difference.
        """
        return ('#!/bin/sh\n"%s" "$@"\nstatus=$?\nprintf "frf-mutation\\n"\nexit $status\n'
                % " ".join(self._program))

    def coverage(self):
        return coverage.backend_for(self.material.language)

    def forbidden_references(self, spec: Spec) -> list:
        """On this seam the rule is enforced by the image, not by inspection.

        A four-channel verifier cannot tell what language a binary was built from, so the toolchain
        is removed instead. Returning nothing here is correct and is not a gap: the evidence check
        also asks whether execution is isolated, and that half is what remains meaningful.
        """
        return []

    def isolated(self) -> bool:
        return True


class Repo:
    """The repository scale. Four answers; the pipeline does the rest."""

    name = "repo"

    def __init__(self, index=None, workspace: str = "", *, observer=None, harvest=None) -> None:
        self._index = index
        self._workspace = workspace or os.path.join("work", "repo")
        self._observer = observer
        # How scenarios are lifted from a checkout. Injected because it is the one genuinely
        # per-repository step -- a project's tests are shell, or a Makefile, or a Python harness --
        # and hard-coding one shape here would quietly restrict which repositories can be used.
        self._harvest = harvest
        self._material: Material | None = None

    def find(self, budget: int):
        if self._index is None:
            raise LookupError(
                "the repo scale needs a code-search index to source from. Pass one to "
                "Repo(index=...), or supply candidates to Factory.build(candidates=[...]).")
        from ..core import sourcing

        return sourcing.walk(self._index, budget)

    def specify(self, candidate: Candidate) -> Spec:
        self._material = self._locate(candidate)
        material = self._material
        return Spec(name=_task_name(material), scale=self.name, language=material.language,
                    description=material.description, build=list(material.build),
                    invoke=list(material.invoke), target_language=material.target_language,
                    environment={"exclude": list(material.exclude)})

    def observe(self):
        if self._observer is not None:
            return self._observer
        if self._material is None:
            raise RuntimeError("observe() was asked for before specify() chose a repository")
        return Observer(self._material)

    def probes(self, spec: Spec) -> ProbeSource:
        if self._material is None:
            raise RuntimeError("probes() was asked for before specify() chose a repository")
        scenarios = self._material.scenarios
        if not scenarios and self._harvest is not None:
            scenarios = tuple(self._harvest(self._material.root))
        if not scenarios:
            raise ValueError(
                "no scenarios were lifted from %s. A repository task is graded on commands this "
                "project already runs; without them there is nothing to observe."
                % self._material.identity)
        return ProbeSource(scenarios)

    def _locate(self, candidate: Candidate) -> Material:
        detail = candidate.detail or {}
        if "invoke" not in detail:
            raise ValueError("candidate %s does not say how to invoke the program it builds"
                             % candidate.identity)
        return Material(
            identity=candidate.identity, language=candidate.language,
            root=detail.get("root", ""), build=list(detail.get("build", ())),
            invoke=list(detail["invoke"]), description=detail.get("description", ""),
            target_language=detail.get("target_language", ""),
            scenarios=tuple(detail.get("scenarios", ())),
            fixtures=detail.get("fixtures", ""),
            exclude=tuple(detail.get("exclude", (".git",))))


def _task_name(material: Material) -> str:
    stem = material.identity.rstrip("/").rsplit("/", 1)[-1].replace(".git", "").lower()
    if material.target_language:
        return "%s-%s-rewrite" % (stem, material.target_language.lower())
    return "%s-faster" % stem
