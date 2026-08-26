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

import asyncio
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Iterable

from .core import pipeline
from .core import sandbox
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
    # Which backend to insist on, or empty for "whichever is available, best first". Named rather
    # than inferred so that a run can REQUIRE the remote one: `sandbox.find` refuses a preference it
    # cannot honour instead of substituting another, and a batch frozen half remotely and half on a
    # local daemon would carry two different meanings under one provenance record.
    backend: str = ""
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
        # Opened on first use and shared by every candidate in the batch. See `backend()`.
        self._backend = None
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
    async def build_async(
        self,
        scale: str,
        *,
        budget: int = 1,
        candidates: Iterable[Candidate] | None = None,
        max_concurrent: int = 32,
        checkpoint_file: str | None = None,
        progress_callback=None,
    ) -> Result:
        """Async version of build(). Runs up to max_concurrent candidates in parallel.

        Graceful shutdown: a SIGINT sets a stop event so no new candidates are started,
        but all already-running ones are allowed to finish cleanly.
        """
        from .core.checkpoint import CheckpointWriter, CheckpointRecord, make_checkpoint_path
        from .core.progress import ProgressReporter
        from datetime import datetime, timezone

        implementation = self._require(scale)
        material = list(candidates if candidates is not None else implementation.find(budget))
        capped = material[:budget]
        total = len(capped)

        # Checkpoint: skip identities already done on a previous run.
        writer: CheckpointWriter | None = None
        already_done: set[str] = set()
        if checkpoint_file is not None:
            cp_path = checkpoint_file or make_checkpoint_path()
            writer = CheckpointWriter(cp_path)
            already_done = writer.load_completed()
            if already_done:
                self.log("[%s] checkpoint: skipping %d already-done candidate(s)"
                         % (scale, len(already_done)))

        reporter = ProgressReporter(total, scale)
        semaphore = asyncio.Semaphore(max_concurrent)
        stop_event = asyncio.Event()
        result = Result(scale)
        result_lock = asyncio.Lock()

        # SIGINT handler — sets stop_event so no new candidates are started.
        loop = asyncio.get_event_loop()
        original_sigint = signal.getsignal(signal.SIGINT)

        def _sigint_handler(signum, frame):
            self.log("\n[%s] interrupted — finishing in-flight candidates, then stopping"
                     % scale)
            loop.call_soon_threadsafe(stop_event.set)

        signal.signal(signal.SIGINT, _sigint_handler)

        # Keep the executor bounded by the same concurrency contract as the semaphore. The
        # previous default created an unbounded thread pool even though only max_concurrent jobs
        # could enter the pipeline; repeated waves of slow remote candidates could therefore
        # retain one thread per submitted candidate and exhaust the host before E2B enforced its
        # own active-sandbox limit.
        executor = ThreadPoolExecutor(max_workers=max(1, max_concurrent))

        async def _build_one(index: int, candidate: Candidate) -> None:
            if stop_event.is_set():
                return
            self.log("[%s] %d/%d %s" % (scale, index + 1, total, candidate.identity))
            await semaphore.acquire()
            try:
                if stop_event.is_set():
                    return
                # CPU-bound pipeline stages run in a thread pool so the event loop stays free.
                hooks = self._hooks(implementation)
                outcome = await loop.run_in_executor(
                    executor,
                    lambda c=candidate, h=hooks: pipeline.build_one(
                        implementation, c, h,
                        freeze_runs=self.settings.freeze_runs, log=self.log))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = "%s: %s" % (type(exc).__name__, exc)
                outcome = pipeline.Refused("unclassified", type(exc).__name__,
                                           pipeline.Fault.FACTORY, detail[:2000])
            finally:
                semaphore.release()

            # Record outcome.
            async with result_lock:
                (result.batch.emitted if outcome.ok else result.batch.refused).append(outcome)

            # Checkpoint write.
            if writer is not None:
                status = "emitted" if outcome.ok else "refused"
                path = getattr(outcome, "path", "")
                stage = getattr(outcome, "stage", "")
                reason = getattr(outcome, "reason", "")
                fault = getattr(getattr(outcome, "fault", None), "value", "")
                record = CheckpointRecord(
                    identity=candidate.identity,
                    scale=scale,
                    task_form="",
                    status=status,
                    stage=stage,
                    reason=reason,
                    fault=fault,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    path=path,
                )
                try:
                    writer.write(record)
                except Exception as exc:
                    self.log("[checkpoint] write failed: %s" % exc)

            # Progress.
            p_status = "emitted" if outcome.ok else "refused"
            p_stage = getattr(outcome, "stage", "")
            reporter.record(p_status, stage=p_stage)
            if progress_callback is not None:
                try:
                    progress_callback(outcome)
                except Exception:
                    pass

        # Skip already-completed candidates.
        pending = [c for c in capped if c.identity not in already_done]
        skipped = len(capped) - len(pending)
        if skipped:
            reporter._done += skipped  # count skipped in progress display

        tasks = [asyncio.create_task(_build_one(i, c)) for i, c in enumerate(pending)]
        try:
            await asyncio.gather(*tasks)
        finally:
            signal.signal(signal.SIGINT, original_sigint)
            executor.shutdown(wait=False)

        summary = result.summary()
        self.log("[%s] %d/%d shipped (%.0f%% yield)%s"
                 % (scale, summary["emitted"], summary["attempted"],
                    100 * summary["yield_rate"],
                    "" if summary["trustworthy"] else
                    "  -- NOT A YIELD: %d refusal(s) were our fault" % summary["refused_factory"]))
        if writer is not None:
            self.log("[checkpoint] written to %s" % writer.path)
        import sys
        print(reporter.final_summary(), file=sys.stderr)
        return result

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
                                         freeze_runs=self.settings.freeze_runs, log=self.log)
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

    # ---------------------------------------------------------------- the sandbox
    def backend(self):
        """The sandbox this run freezes in, opened once and shared. -> a Backend, or None.

        OPENED ONCE PER FACTORY, not per candidate. A remote sandbox costs seconds and money to
        start, and a batch of twenty would otherwise pay for twenty of them; the scales are handed
        the same one so that every expectation in a batch was frozen in the same place.

        `Settings.sandboxed` used to be documented as load-bearing and read by nothing, which made
        it worse than absent: a run could say `sandboxed=True` in its provenance while every stage
        ran on the factory's own host. This is the method that gives that setting a mechanism.
        """
        if not self.settings.sandboxed:
            return None
        if self._backend is None:
            self._backend = sandbox.find(prefer=self.settings.backend or None)
            self.log("[sandbox] %s" % self._backend.name)
        return self._backend

    def close(self) -> None:
        """Release the sandbox, if one was opened. Safe to call more than once."""
        if self._backend is not None:
            self._backend.close()
            self._backend = None

    def __enter__(self) -> "Factory":
        return self

    def __exit__(self, *_) -> None:
        self.close()

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
