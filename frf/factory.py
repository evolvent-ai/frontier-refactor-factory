"""The one object a user of this library holds.

Everything below this file is machinery. This is the surface, and it is deliberately narrow:

    Factory()                       every scale, default settings
    factory.build("module", 20)     try twenty candidates at one scale
    factory.build_all(20)           twenty at each registered scale
    factory.register(MyScale())     a fifth scale, without editing this package

WHY SETTINGS IS A DATACLASS AND NOT KEYWORD ARGUMENTS. A run's configuration ends up in the
provenance of every task it produces, and a dataclass can be written there whole. Spreading the same
knobs across a constructor signature makes "what settings produced this task" a question nobody can
answer six months later.

WHY THE SCALE REGISTRY IS NOT HARD-CODED. `register` exists so that adding a scale is something a
reader of this library can do without forking it -- and, more importantly, so that the claim in
`core/scale.py` stays testable: a new scale must not require editing anything in `core/`. A registry
that only ever held four entries written in this file would make that claim unfalsifiable.

WHAT THIS OBJECT DOES NOT DO. It does not decide what makes a good task; the gates do, and they live
next to the evidence they enforce. It does not know what an observation is. It holds the settings,
the registry and a logger, and hands them to the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable

from .core import pipeline
from .core.scale import SCALES, Candidate, Scale


@dataclass(frozen=True)
class Settings:
    """How a run behaves. Written into every task's provenance, so it must be worth reading.

    `sandboxed` defaults to True and should stay that way outside a debugging session. It is not
    about safety alone: the expectation has to be frozen inside the image the task will ship with,
    because an expectation frozen against the factory's own toolchain describes a program the solver
    will never receive. Turning it off is honest only when you are inspecting a stage, never when
    you are producing a task anyone will attempt.
    """

    sandboxed: bool = True
    freeze_runs: int = pipeline.FREEZE_RUNS
    min_probes: int = pipeline.MIN_PROBES
    min_graded_points: int = pipeline.MIN_GRADED_POINTS
    output_dir: str = "tasks"
    languages: tuple = ()                      # empty means "whatever the index offers"

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    """What one call to `build` produced.

    Wraps a `Batch` rather than replacing it: the yield and the material/factory split are computed
    there, next to the reasoning about why the split matters.
    """

    scale: str
    batch: pipeline.Batch = field(default_factory=pipeline.Batch)

    @property
    def tasks(self) -> list:
        return self.batch.emitted

    def summary(self) -> dict:
        return dict(self.batch.summary(), scale=self.scale)

    def __len__(self) -> int:
        return len(self.batch.emitted)

    def __bool__(self) -> bool:
        return bool(self.batch.emitted)


class Factory:
    """Produces refactoring tasks from real code.

    Holds no state about a run beyond its settings and its registry; two factories with the same
    settings are interchangeable, and a run can be reproduced from what is written into provenance.
    """

    def __init__(self, settings: Settings | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.settings = settings or Settings()
        self.log = log or (lambda _message: None)
        self._scales: dict = {}
        # Shared stage implementations, keyed by name. Empty until a seam installs them, which is
        # what `install_stages` is for -- keeping them here rather than importing a fixed set is
        # what lets the two seams supply different freezes without the pipeline knowing.
        self._defaults: dict = {}

    # ---------------------------------------------------------------- registry
    def register(self, scale: Scale) -> "Factory":
        """Add a scale. Returns self, so registrations chain.

        A scale whose name is not one of the four is allowed: the four are what this library ships,
        not what the design permits. What is NOT allowed is silently replacing one, because a run
        that used a different implementation than its provenance claims is unreproducible.
        """
        name = getattr(scale, "name", None)
        if not name:
            raise ValueError("a scale must have a name")
        if name in self._scales:
            raise ValueError("scale %r is already registered; unregister it first if that is "
                             "intended" % name)
        self._scales[name] = scale
        return self

    def install_stages(self, **stages) -> "Factory":
        """Supply the shared stage implementations this factory drives.

        Separate from `register` because stages belong to a SEAM and scales belong to a family: the
        repo scale and a package scale share their freeze exactly when they share a seam, so binding
        stages to scales would make each scale reimplement half a pipeline.
        """
        unknown = set(stages) - {"build", "freeze", "adequacy", "battery", "emit", "replay"}
        if unknown:
            raise ValueError("unknown stage(s): %s" % ", ".join(sorted(unknown)))
        self._defaults.update(stages)
        return self

    def unregister(self, name: str) -> "Factory":
        self._scales.pop(name, None)
        return self

    @property
    def scales(self) -> list:
        """Which scales this factory can build, in the canonical order."""
        known = [s for s in SCALES if s in self._scales]
        extra = sorted(n for n in self._scales if n not in SCALES)
        return known + extra

    # ---------------------------------------------------------------- building
    def build(self, scale: str, budget: int = 1, *,
              candidates: Iterable[Candidate] | None = None) -> Result:
        """Try `budget` candidates at one scale. -> what shipped, and why the rest did not.

        `candidates` overrides sourcing, which is what makes a specific piece of material
        reproducible: passing the same candidate twice must produce the same task, and a bug report
        that says "this repository fails" needs a way to say only that repository.
        """
        implementation = self._require(scale)
        material = candidates if candidates is not None else implementation.find(budget)

        result = Result(scale)
        for index, candidate in enumerate(material):
            if index >= budget:
                break
            self.log("[%s] %d/%d %s" % (scale, index + 1, budget, candidate.identity))
            outcome = pipeline.build_one(implementation, candidate, self._hooks(implementation),
                                         log=self.log)
            (result.batch.emitted if outcome.ok else result.batch.refused).append(outcome)

        summary = result.summary()
        self.log("[%s] %d/%d shipped (%.0f%% yield)%s"
                 % (scale, summary["emitted"], summary["attempted"],
                    100 * summary["yield_rate"],
                    "" if summary["trustworthy"] else
                    "  -- NOT A YIELD: %d refusal(s) were our fault" % summary["refused_factory"]))
        return result

    def build_all(self, budget: int = 1) -> dict:
        """`budget` candidates at every registered scale. -> {scale: Result}."""
        return {name: self.build(name, budget) for name in self.scales}

    # ---------------------------------------------------------------- internals
    def _require(self, scale: str) -> Scale:
        if scale not in self._scales:
            raise LookupError(
                "no implementation registered for scale %r. Registered: %s. Use "
                "Factory().register(...) to add one."
                % (scale, ", ".join(self.scales) or "(none)"))
        return self._scales[scale]

    def _hooks(self, scale: Scale) -> pipeline.Hooks:
        """The six stages the pipeline drives, resolved for this scale.

        These are SHARED implementations, not per-scale ones -- that is the entire claim of this
        design. A scale answers four questions (`find`, `specify`, `observe`, `probes`) and supplies
        none of these six.

        A scale may nevertheless override one, and the lookup below is what makes that possible. It
        is meant for the case where a scale genuinely differs and the difference has been argued
        for, not as a convenience: an override is a claim that the shared stage is wrong here, and
        it should be as visible in review as any other claim.
        """
        def resolve(stage: str):
            override = getattr(scale, stage, None)
            if override is not None:
                return override
            default = self._defaults.get(stage)
            if default is not None:
                return default
            raise NotImplementedError(
                "stage %r has no implementation. The shared stages are installed by the seam a "
                "scale observes through; scale %r supplied neither an override nor a seam that "
                "provides one." % (stage, getattr(scale, "name", scale)))

        return pipeline.Hooks(build=resolve("build"), freeze=resolve("freeze"),
                              adequacy=resolve("adequacy"), battery=resolve("battery"),
                              emit=resolve("emit"), replay=resolve("replay"))
