"""Small, explicit batch entry points for real registry-backed runs.

This module owns wiring, not judgement: it selects an enumerable index, registers the requested
scale, installs the matching observation seam, and records elapsed time. Package candidates still
need a generator/entry-point proposal because a registry does not publish a callable contract.
"""
from __future__ import annotations

import tempfile
import os
import time
import hashlib
import threading
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import source
from .factory import Factory, Settings
from .scales import Kernel, Module, Package, Repo
from .observe.call import stages as call_stages
from .observe.probes.generator import run_in as run_generator_in
from .observe.process import stages as process_stages
from .observe import checkout_task
from .core.contract import CheckoutContract
from .core.ledger import BatchLedger, LedgerRecord
from .core.diversity import DiversityPolicy
from .core.capabilities import capability
from .core.harbor import Package as HarborPackage
from .core import pipeline
from .core.scale import TaskForm

_E2B_ACTIVE_LIMIT = max(1, int(os.environ.get("FRF_E2B_MAX_ACTIVE", "8")))
_E2B_SLOTS = threading.BoundedSemaphore(_E2B_ACTIVE_LIMIT)


def configure_e2b_slots(limit: int) -> None:
    """Set the live-sandbox limit before a configured run starts."""
    global _E2B_ACTIVE_LIMIT, _E2B_SLOTS
    limit = max(1, int(limit))
    _E2B_ACTIVE_LIMIT = limit
    _E2B_SLOTS = threading.BoundedSemaphore(limit)


@dataclass(frozen=True)
class BatchReport:
    """A serialisable summary for one automated scale run."""

    summary: dict
    seconds: float
    index: str

    def to_json(self) -> dict:
        return {**self.summary, "seconds": round(self.seconds, 3), "index": self.index}


def emit_checkout_task(*, destination: str, package: HarborPackage,
                       contract: CheckoutContract) -> tuple[int, int]:
    """Production entry point for a checkout-native task.

    The task is rejected if its own hidden-reference replay cannot execute every declared native
    build/verification command. This is intentionally separate from the legacy call-seam batch.
    """
    checkout_task.write(destination, package, contract)
    passed, total = checkout_task.drive(destination)
    if passed != total:
        raise RuntimeError("checkout task does not reproduce itself (%d/%d)" % (passed, total))
    return passed, total


_FORM_MAP = {
    "inplace": TaskForm.INPLACE,
    "cross": TaskForm.CROSS_LANGUAGE,
}

# What a repo-scale corpus can actually grade: programs that READ something and WRITE a determined
# result. A four-channel freeze needs stdout that repeats, which rules out anything that redraws a
# screen or asks the network. These are the topics maintainers give such programs, in rough order
# of how reliably the label holds.
#
# MEASURED, NOT SUPPOSED: `topic:cli` returned dive and lazygit (TUIs) and httpx (a network
# scanner), and all three produced zero liftable invocations.
TRANSFORMER_TOPICS = ("jq", "yq", "csvkit", "pandoc", "file-converter", "json-parser", "csv-parser", "data-processing",
                      "json", "csv", "xml", "etl", "data-science", "data-conversion",
                      "text-processing", "parser", "compiler", "transpiler", "minifier",
                      "formatter", "linter", "cli", "command-line")


def _chain_of_topics(cls, topics: tuple, language: str):
    """One index per topic, walked end to end.

    GitHub answers 422 to `topic:a OR topic:b`, so several topics is several searches. Chaining
    them keeps `sourcing.walk`'s contract intact: one index, one denominator, empty only when
    every topic is spent.
    """
    from .source.chain import Chain

    return Chain([cls(language=language, query="topic:%s" % topic, scale="repo")
                  for topic in topics],
                 name="github(%s)" % "|".join(topics))


def _merge_reports(reports, index_name, elapsed):
    summary = {"attempted": 0, "emitted": 0, "yield_rate": 0.0,
               "refused_material": 0, "refused_factory": 0,
               "trustworthy": True, "by_reason": {}, "source_rejections": {}}
    for report in reports:
        item = report.summary
        for key in ("attempted", "emitted", "refused_material", "refused_factory"):
            summary[key] += item.get(key, 0)
        summary["trustworthy"] = summary["trustworthy"] and item.get("trustworthy", False)
        for reason, count in item.get("by_reason", {}).items():
            summary["by_reason"][reason] = summary["by_reason"].get(reason, 0) + count
        for reason, count in item.get("source_rejections", {}).items():
            summary["source_rejections"][reason] = summary["source_rejections"].get(reason, 0) + count
    summary["yield_rate"] = round(summary["emitted"] / summary["attempted"], 4) if summary["attempted"] else 0.0
    summary["scale"] = reports[0].summary.get("scale", "") if reports else ""
    if not summary["source_rejections"]:
        summary.pop("source_rejections")
    walked = fresh = repeats = 0
    totals = []
    for report in reports:
        source_stats = report.summary.get("sourcing") or {}
        walked += int(source_stats.get("walked", 0))
        fresh += int(source_stats.get("fresh", 0))
        repeats += int(source_stats.get("repeats", 0))
        if source_stats.get("total") is not None:
            totals.append(int(source_stats["total"]))
    if walked or fresh or repeats:
        summary["sourcing"] = {"index": index_name, "walked": walked, "fresh": fresh,
                                "repeats": repeats, "total": sum(totals) if totals else None,
                                "remaining": (max(0, sum(totals) - walked) if totals else None)}
    return BatchReport(summary, elapsed, index_name)


def run(scale: str, *, budget: int = 1, index: str | None = None,
        output_dir: str = "tasks", backend: str = "remote", subset: str = "",
        form: str = "inplace", target_language: str = "",
        freeze_runs: int = pipeline.FREEZE_RUNS, candidates=None,
        candidate_workers: int = 1, ledger_file: str = "", harbor_check: bool = False,
        harbor_repair: bool = True, harbor_max_repairs: int = 1,
        target_emitted: bool = False, max_attempts: int = 0) -> BatchReport:
    """Run one scale from an automatically selected enumerable index.

    The returned report separates the pipeline summary from elapsed wall time. The call/process
    seam is selected from the scale, and the factory is required to use the requested backend.

    `form` is converted to a TaskForm and stored on the scale instance so that the pipeline can
    forward it to specify() without the factory needing a new API surface.  `target_language` is
    accepted here for API symmetry (it travels in the Spec, not in specify()'s signature).

    `subset` DEFAULTS TO EMPTY, and that is a correction rather than a preference. It is a
    substring filter over package NAMES, so `subset="algorithm"` -- the previous default -- reduced
    the pond to packages that call themselves algorithms, which in practice means teaching
    collections: factorials, leap-year tests, odd/even checks. Those are exactly the subjects the
    timing gate then refuses, because their cost does not vary with input at all.

    The index already ranks computational-looking names first (`filters.looks_computational`), and
    ranking is the right tool here: it puts sort/hash/compress/parse/matrix packages at the front
    of the queue WITHOUT dropping anything, so the remaining supply is still countable. A filter
    that silently shrinks the pond makes a yield figure meaningless, because the denominator is no
    longer the supply.
    """
    task_form = _FORM_MAP.get(form.strip().lower(), TaskForm.INPLACE)
    name = scale.strip().lower()
    # Concurrent candidate workers may discover the same human-readable task name across pinned
    # revisions. Keep the Harbor task name stable, but isolate each candidate's output tree so one
    # worker cannot overwrite another worker's reference/expectations during replay.
    if candidates is not None:
        candidate_list = list(candidates)
        candidates = candidate_list
        if len(candidate_list) == 1:
            identity = str(getattr(candidate_list[0], "identity", "candidate"))
            # The same sourced subject may legitimately produce Module and Kernel tasks. Include
            # scale/form in the workspace key so one successful run cannot overwrite another
            # scale's emitted reference and verifier while retaining the readable task name.
            isolation_key = "%s|%s|%s|%s" % (name, form, target_language, identity)
            suffix = hashlib.sha256(isolation_key.encode("utf-8")).hexdigest()[:12]
            output_dir = os.path.join(output_dir, ".candidates", suffix)
    # Empty `index` means automatic selection, including when it came from JobConfig.index="".
    # An explicit non-empty value is honored exactly.
    if index:
        index_name = index
    elif name in ("module", "kernel"):
        index_name = "github-functions"
    elif name == "package":
        index_name = "github-packages"
    else:
        index_name = "github"
    if target_emitted and candidates is None:
        # Configured roll mode: keep sourcing until the requested number has passed every gate.
        # A finite attempt limit is part of the contract: a depleted or low-yield source must end
        # with auditable evidence, not an unbounded production run.
        target = budget
        attempt_limit = max_attempts or max(target, target * 10)
        source_index = _index(index_name, subset=subset, scale=name)
        source_scale = _scale(name, source_index, backend=None, workspace=tempfile.mkdtemp(prefix="frf-source-%s-" % name))
        diversity = DiversityPolicy(max_per_repository=4)
        started = time.perf_counter()
        reports = []
        seen = set()
        requested = min(attempt_limit, max(4, target * 3))
        attempted = 0
        while (sum(r.summary.get("emitted", 0) for r in reports) < target
               and attempted < attempt_limit):
            batch = list(source_scale.find(requested))
            unseen = [c for c in batch if c.identity not in seen]
            if not unseen:
                if len(batch) < requested:
                    break
                if requested >= attempt_limit:
                    break
                requested = min(attempt_limit, requested + max(4, target * 2))
                continue
            remaining_tasks = target - sum(r.summary.get("emitted", 0) for r in reports)
            room = attempt_limit - attempted
            # One candidate can emit at most one task. Restrict each wave to the remaining target
            # so parallel completion cannot overshoot it.
            wave = []
            for candidate in unseen:
                if len(wave) >= min(remaining_tasks, room):
                    break
                seen.add(candidate.identity)
                if diversity.accept(candidate.identity):
                    wave.append(candidate)
            if not wave:
                continue
            attempted += len(wave)
            with ThreadPoolExecutor(max_workers=max(1, candidate_workers)) as pool:
                futures = [pool.submit(run, name, budget=1, index=index_name,
                                       output_dir=output_dir, backend=backend, subset=subset,
                                       form=form, target_language=target_language,
                                       freeze_runs=freeze_runs, candidates=[candidate],
                                       ledger_file=ledger_file,
                                       harbor_check=harbor_check, harbor_repair=harbor_repair,
                                       harbor_max_repairs=harbor_max_repairs)
                           for candidate in wave]
                reports.extend(future.result() for future in as_completed(futures))
        merged = _merge_reports(reports, index_name, time.perf_counter() - started)
        merged.summary["target_emitted"] = target
        merged.summary["target_met"] = merged.summary.get("emitted", 0) >= target
        merged.summary["max_attempts"] = attempt_limit
        return merged

    # Worker concurrency and active sandbox concurrency are separate controls. Keep many workers
    # queued for throughput, but cap live E2B sandboxes to the account/template memory envelope.
    e2b_slot = backend == "remote"
    if e2b_slot:
        _E2B_SLOTS.acquire()
    try:
        factory = Factory(Settings(sandboxed=True, backend=backend, output_dir=output_dir,
                                   freeze_runs=freeze_runs), log=print)
    except Exception:
        if e2b_slot:
            _E2B_SLOTS.release()
        raise
    # Bind the actual selected backend into the scale before any observer is built. Constructing
    # the scale first silently made automatic runs use local execution even when Settings required
    # remote E2B.
    idx = _index(index_name, subset=subset, scale=name)
    workspace = tempfile.mkdtemp(prefix="frf-%s-" % name)
    implementation = _scale(name, idx, backend=factory.backend(), workspace=workspace)
    # Store task_form on the instance so pipeline._specify() can pick it up without a
    # factory-level API change. The attribute is advisory: scales whose specify() accepts
    # task_form will receive it; others fall back to their own default.
    implementation._task_form = task_form
    if target_language:
        implementation._target_language = target_language
    factory.register(implementation)

    # SEAM SELECTION, and the writer belongs to the SEAM rather than to the scale.
    #   module/kernel/package -> call seam (接缝 B, JSON 行协议)
    #   repo                  -> process seam (接缝 A, 进程四通道)
    #
    # The two seams package a task differently, so they have different writers. On the call seam
    # the writer is a module-level function taking the spec and the located material, because a
    # served subject has to be NAMED (language plus symbol) for the shipped verifier to serve it
    # again -- and neither is knowable from the corpus alone. Looking for a `write_tests` METHOD on
    # the scale, as the process seam has, refused every call-seam scale for lacking a writer that
    # was never supposed to be there.
    if name in ("module", "kernel", "package"):
        from .observe.call import package as call_package

        def writer(path, corpus, _scale=implementation):
            # Read at emit time, not captured at install time: `specify()` replaces the material
            # for each candidate, so binding it now would package every task after the first with
            # the first candidate's symbol.
            material = _scale._material
            if material is None:
                raise RuntimeError("emit reached the writer before specify chose a subject")
            # The writer needs only the language off the spec, and the material carries the same
            # one -- it is where the spec got it. Passing the material twice keeps this wiring
            # from depending on a `_spec` attribute that the call-seam scales do not keep.
            call_package.write_tests(path, corpus, spec=material, material=material)

        seam = call_stages.Seam(implementation, destination=output_dir,
                                write_tests=writer,
                                drive=lambda path: call_package.drive(path, backend=factory.backend()))
    else:
        method = getattr(implementation, "write_tests", None)
        if method is None:
            raise RuntimeError(
                "scale %s has no Harbor writer; refusing to emit an incomplete task" % name)
        seam = process_stages.Seam(implementation, destination=output_dir,
                                   write_tests=method, drive=implementation.drive)
    factory.install_stages(**seam.stages())
    started = time.perf_counter()
    try:
        result = factory.build(name, budget, candidates=candidates)
        summary = result.summary()
        summary["capability"] = capability(subset or os.environ.get("FRF_REPO_LANGUAGE", "unknown"),
                                            scale=name).__dict__
        elapsed_so_far = time.perf_counter() - started
        summary["metrics"] = {"scale": name, "batch_seconds": round(elapsed_so_far, 3),
                               "seconds_per_attempt": round(elapsed_so_far / max(1, summary.get("attempted", 0)), 3)}
        if harbor_check:
            passed = failed = 0
            harbor_failures = []
            attempts = 1 + (harbor_max_repairs if harbor_repair else 0)
            for outcome in result.batch.emitted:
                ok = False
                detail = ""
                for _ in range(max(1, attempts)):
                    if _ and harbor_repair:
                        repair_command = [os.path.join(os.path.dirname(os.path.dirname(__file__)), ".venv", "bin", "python"),
                                          os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "harbor_check_e2b.py"),
                                          outcome.path, "--model", os.environ.get("LLM_MODEL", "gpt-5.6-terra"), "--repair", "--repair-only"]
                        subprocess.run(repair_command, capture_output=True, text=True, timeout=1800,
                                       env=dict(os.environ))
                    command = [os.path.join(os.path.dirname(os.path.dirname(__file__)), ".venv", "bin", "python"),
                               os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "harbor_check_e2b.py"),
                               outcome.path, "--model", os.environ.get("LLM_MODEL", "gpt-5.6-terra")]
                    check = subprocess.run(command, capture_output=True, text=True, timeout=1800,
                                           env=dict(os.environ))
                    ok = check.returncode == 0
                    detail = (check.stdout + check.stderr)[-1000:]
                    if ok:
                        break
                if ok:
                    passed += 1
                else:
                    failed += 1
                    harbor_failures.append({"path": outcome.path, "detail": detail})
            summary["harbor_checked"] = passed + failed
            summary["harbor_passed"] = passed
            summary["harbor_failed"] = failed
            if harbor_failures:
                summary["harbor_failures"] = harbor_failures
                # Harbor is a production gate.  Removing the count from the summary alone left
                # failed packages in `result.batch.emitted`, so callers iterating the Result (and
                # ledger writers) could still treat them as shipped.  Keep the public result and
                # its summary consistent: only tasks that pass the configured review remain
                # emitted.
                failed_paths = {item["path"] for item in harbor_failures}
                result.batch.emitted[:] = [item for item in result.batch.emitted
                                           if getattr(item, "path", "") not in failed_paths]
                summary["emitted"] = len(result.batch.emitted)
        if ledger_file:
            ledger = BatchLedger(ledger_file)
            for outcome in result.batch.emitted + result.batch.refused:
                ledger.append(LedgerRecord(
                    identity=getattr(outcome, "identity", getattr(outcome, "name", "")),
                    scale=name, status="emitted" if outcome.ok else "refused",
                    stage=getattr(outcome, "stage", ""), reason=getattr(outcome, "reason", ""),
                    fault=getattr(getattr(outcome, "fault", None), "value", ""),
                    path=getattr(outcome, "path", ""),
                    seconds=round(elapsed_so_far / max(1, summary.get("attempted", 0)), 3)))
        coverage = getattr(idx, "last_coverage", None)
        if coverage is not None:
            summary["sourcing"] = coverage.to_json()
        if hasattr(idx, "repositories_walked"):
            summary.setdefault("sourcing", {})["repositories_walked"] = idx.repositories_walked
            summary["sourcing"]["functions_walked"] = idx.functions_walked
        rejections = getattr(idx, "rejection_counts", None)
        if rejections is not None:
            summary["source_rejections"] = dict(sorted(rejections.items()))
        if summary.get("attempted", 0) == 0:
            # An empty enumerable source is not a zero-yield quality result. Preserve the
            # distinction so matrix consumers can tell "no material was supplied" from candidates
            # rejected by build/freeze/adequacy gates.
            summary["source_eligibility"] = "empty"
            summary["source_total"] = idx.total() if hasattr(idx, "total") else None
            summary["source_note"] = "index returned no eligible candidates"
        return BatchReport(summary, time.perf_counter() - started, index_name)
    finally:
        factory.close()
        if e2b_slot:
            _E2B_SLOTS.release()


def _index(name: str, *, subset: str, scale: str = ""):
    cls = source.index_for(name)
    language = subset.strip().lower() if subset else ""

    if name == "github-functions":
        # GitHubFunctions wraps a GitHub index — construct the inner one first.
        # Use topic:algorithms which is a valid single-qualifier GitHub search term.
        query = "topic:algorithms language:python" if scale == "kernel" else "topic:algorithms"
        github = source.GitHub(language=language, query=query, scale="module")
        return cls(github, scale=scale, log=lambda message: print("[source] " + message, flush=True))

    if name == "github-packages":
        if language:
            return cls(source.GitHub(language=language, query="topic:algorithms", scale="package"))
        # Open-world default: enumerate multiple language sources instead of silently restricting
        # package discovery to Python. Unsupported adapters remain explicit source rejections.
        package_languages = ("python", "javascript", "typescript", "rust", "go", "ruby", "java")
        return cls(source.Chain(
            [source.GitHub(language=item, query="topic:algorithms", scale="package")
             for item in package_languages],
            name="github-packages(" + "|".join(package_languages) + ")"))

    if name == "github":
        if scale in ("repo",):
            # WHAT THIS SCALE CAN ACTUALLY GRADE is narrower than "a CLI", and `topic:cli` was
            # measured returning the wrong half of it: dive and lazygit are TUIs whose output is a
            # redrawn screen, and httpx talks to the network -- none of the three yielded a single
            # liftable invocation, because there is no repeatable stdout to freeze.
            #
            # What a four-channel corpus needs is a TRANSFORMER: something that reads a file or
            # stdin and writes a determined result. Those repositories describe themselves with
            # these topics, so they are asked for BY NAME rather than filtered after a clone -- a
            # topic is the maintainer's own statement of what the program is, and it is the
            # cheapest honest signal available before paying for a checkout.
            #
            # ONE QUERY PER TOPIC, chained. GitHub answers 422 to `topic:a OR topic:b`, so several
            # topics means several searches; `Chain` walks them end to end so that `walk()` still
            # sees a single index with a single denominator.
            # No implicit language conversion: repo tasks are native-language by default.  A
            # caller may constrain sourcing with `source_language` or FRF_REPO_LANGUAGE; otherwise
            # GitHub's result language is preserved and the selected E2B image must provide it.
            return _chain_of_topics(cls, TRANSFORMER_TOPICS,
                                    language or os.environ.get("FRF_REPO_LANGUAGE", ""))
        elif scale in ("package",):
            # Package scale needs algorithm/library repos.
            query = "topic:algorithms"
        else:
            query = ""
        if language and query:
            return cls(language=language, query=query)
        elif language:
            return cls(language=language)
        elif query:
            return cls(query=query)
        return cls()

    return cls()


def _scale(name: str, idx, *, backend=None, workspace=""):
    if name == "module":
        return Module(index=idx, workspace=workspace, backend=backend)
    if name == "kernel":
        return Kernel(index=idx, workspace=workspace, backend=backend)
    if name == "package":
        return Package(index=idx, workspace=workspace, backend=backend,
                       run_generator=(lambda source, count, b=backend: run_generator_in(b, source, count)))
    if name == "repo":
        return Repo(index=idx, backend=backend)
    raise ValueError("unknown scale %r; expected kernel, module, package, or repo" % name)
