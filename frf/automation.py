"""Small, explicit batch entry points for real registry-backed runs.

This module owns wiring, not judgement: it selects an enumerable index, registers the requested
scale, installs the matching observation seam, and records elapsed time. Package candidates still
need a generator/entry-point proposal because a registry does not publish a callable contract.
"""
from __future__ import annotations

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
from .core import scratch
from .core.contract import CheckoutContract
from .core.ledger import BatchLedger, LedgerRecord
from .core.diversity import DiversityPolicy
from .core.capabilities import capability
from .core.harbor import Package as HarborPackage
from .core import pipeline
from .core import model as model_usage
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
# screen or asks the network. These are the topics maintainers give such programs.
#
# MEASURED, NOT SUPPOSED: `topic:cli` returned dive and lazygit (TUIs) and httpx (a network
# scanner), and all three produced zero liftable invocations. So `cli` and `command-line` sit last.
#
# ORDER IS SUPPLY, NOT ONLY RELIABILITY, and getting that backwards cost a whole batch. `Chain` is
# deliberately depth-first: the first link is spent before the second is touched, so a budget is
# consumed in list order. An earlier version led with the most precisely-labelled topics -- jq, yq,
# csvkit, pandoc -- which is correct about the labels and wrong about the outcome, because those
# topics barely exist outside Python. Counted on GitHub with `archived:false mirror:false stars:>=10`:
#
#             jq   yq  csvkit  pandoc  file-converter | parser  compiler  formatter  linter
#   rust      21    3      0       5         2        |    304       425         63      89
#   go       ---- first six total 33 ----------------  |  ---- last four total 538 ----
#   java     ---- first six total 12 ----------------  |  ---- last four total 324 ----
#
# A budget of 3 gets `attempt_limit = 30`; jq's 21 Rust repositories plus yq and pandoc exhaust it
# before `parser` is ever queried. A real Rust batch refused 13 candidates that way and every one of
# them was a jq clone -- several of them TUIs, which this list exists to avoid. The batch could not
# have reached the topics most likely to work.
#
# So the plentiful transformers lead. `parser`, `compiler`, `transpiler`, `formatter`, `linter` and
# `minifier` are transformers BY DEFINITION -- each reads a text and writes a determined text -- and
# they are also where the supply is. The precise-but-tiny topics keep their place afterwards: they
# cost little to walk once the batch has already had its best chance.
TRANSFORMER_TOPICS = ("parser", "compiler", "transpiler", "formatter", "linter", "minifier",
                      "text-processing", "data-processing", "json", "csv", "xml", "etl",
                      "json-parser", "csv-parser", "data-conversion", "data-science",
                      "jq", "yq", "csvkit", "pandoc", "file-converter",
                      "cli", "command-line")

# The topics whose supply was measured too small to lead a walk. Named so the ordering property can
# be tested rather than trusted to survive the next edit.
_SCARCE_TOPICS = frozenset(("jq", "yq", "csvkit", "pandoc", "file-converter",
                            "csv-parser", "data-conversion"))


# What a function-scale corpus (kernel/module/package) can freeze: single functions whose inputs
# and outputs are JSON-serialisable. `topic:algorithms` alone was the whole supply for all three
# scales, which concentrated output on algorithm-puzzle repositories -- the same supply is every
# night's harvest, and a solver asked to "make it faster" on the twentieth LeetCode clone is
# testing nothing the first nineteen didn't. A function corpus wants diversity for the same
# reason a repo corpus does, so several TOPICS are chained. Order is by measured supply: the
# algorithmic topics hold most of what the call seam can serve, and each later topic widens the
# corpus toward a different family of code -- strings, numbers, dates, structures, geometry,
# text processing.
#
# Measured with archived:false mirror:false stars:>=10 per language (2026-08-27):
#   python 2054, cpp 1649, javascript 1255, java 963, typescript 756, rust 117, ruby 67.
FUNCTION_TOPICS = ("algorithms", "data-structures", "string", "math", "matrix",
                   "graph", "text-processing", "datetime", "geometry")


def _chain_of_topics(cls, topics: tuple, language: str, *, scale: str = "repo",
                     quota: int = 0):
    """One index per topic, walked end to end -- or round-robin, for a diverse corpus.

    `quota=0` (the default, used by the repo scale) keeps the depth-first `Chain`: the links are
    in preference order and a small budget spends itself on the first, most likely topic.

    `quota>0` (used by the function scales) returns `QuotaChain`, which gives every topic its
    turn in bounded pages. With nine function topics and a budget of three, the depth-first walk
    spent the whole batch on `algorithms` and never reached `string` or `math`; the corpus was
    as concentrated as the single search this replaces, just later.

    GitHub answers 422 to `topic:a OR topic:b`, so several topics is several searches. Either way
    `sourcing.walk` sees one index, one denominator, empty only when every topic is spent.
    """
    from .source.chain import Chain, QuotaChain

    links = [cls(language=language, query="topic:%s" % topic, scale=scale)
             for topic in topics]
    name = "github(%s)" % "|".join(topics)
    return Chain(links, name=name) if quota <= 0 else QuotaChain(links, quota=quota, name=name)


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
        target_emitted: bool = False, max_attempts: int = 0,
        max_per_repository: int = 4) -> BatchReport:
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
        source_scale = _scale(name, source_index, backend=None, workspace=scratch.mkdtemp(prefix="frf-source-%s-" % name))
        # CONFIGURABLE, because the right concentration differs by scale. A high-yield
        # scale like module can fill a corpus from a handful of generous repositories
        # and wants a tight cap; a 14%-yield scale spreads far wider than the number
        # suggests, because the cap counts ATTEMPTS, and tightening it there only
        # starves the batch.
        diversity = DiversityPolicy(max_per_repository=max_per_repository)
        started = time.perf_counter()
        reports = []
        seen = set()
        # Candidates lost to our own infrastructure rather than to the pipeline's gates.
        # Surfaced in the summary so a thin batch cannot be read as thin material.
        infrastructure_failures = 0
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
                # ONE CANDIDATE MUST NOT END THE JOB. A roll asks for twenty-five tasks and runs
                # each candidate in its own `run()`; letting that call's exception out of
                # `future.result()` ends the whole roll. A live batch died exactly there -- a DNS
                # blip reaching api.e2b.app raised SandboxError on ONE candidate and took a
                # twenty-five task job down with it, discarding the fourteen already emitted from
                # that job's accounting.
                #
                # This is the same rule the pipeline already applies inside a candidate: the wire is
                # not the material. A candidate we could not even start is charged to us, counted as
                # attempted, and the roll moves on -- which is also what keeps the yield honest,
                # since a job that stops early looks identical to one whose supply ran out.
                for future in as_completed(futures):
                    try:
                        reports.append(future.result())
                    except Exception as exc:                   # noqa: BLE001 -- reported, not raised
                        print("[%s] candidate failed outside the pipeline (%s: %s); continuing"
                              % (name, type(exc).__name__, str(exc)[:200]), flush=True)
                        infrastructure_failures += 1
        merged = _merge_reports(reports, index_name, time.perf_counter() - started)
        merged.summary["target_emitted"] = target
        merged.summary["target_met"] = merged.summary.get("emitted", 0) >= target
        merged.summary["max_attempts"] = attempt_limit
        if infrastructure_failures:
            merged.summary["infrastructure_failures"] = infrastructure_failures
        return merged

    # Worker concurrency and active sandbox concurrency are separate controls. Keep many workers
    # queued for throughput, but cap live E2B sandboxes to the account/template memory envelope.
    # One candidate's spend, counted from here. Each roll worker runs `run()` on its own thread and
    # writes its own ledger row, so resetting at the top of the call is what keeps one candidate's
    # generator out of another's total.
    model_usage.reset_usage()
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
    workspace = scratch.mkdtemp(prefix="frf-%s-" % name)
    implementation = _scale(name, idx, backend=factory.backend(), workspace=workspace)
    # Store task_form on the instance so pipeline._specify() can pick it up without a
    # factory-level API change. The attribute is advisory: scales whose specify() accepts
    # task_form will receive it; others fall back to their own default.
    implementation._task_form = task_form
    if target_language:
        implementation._target_language = target_language
    _refuse_a_form_nothing_will_honour(implementation, name, task_form, target_language)
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
                               "seconds_per_attempt": round(elapsed_so_far / max(1, summary.get("attempted", 0)), 3),
                               "backend_name": getattr(factory.backend(), "name", "none"),
                               "backend_type": type(factory.backend()).__name__}
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
            spent = model_usage.usage_so_far()
            ledger = BatchLedger(ledger_file)
            for outcome in result.batch.emitted + result.batch.refused:
                ledger.append(LedgerRecord(
                    identity=getattr(outcome, "identity", getattr(outcome, "name", "")),
                    scale=name, status="emitted" if outcome.ok else "refused",
                    stage=getattr(outcome, "stage", ""), reason=getattr(outcome, "reason", ""),
                    fault=getattr(getattr(outcome, "fault", None), "value", ""),
                    path=getattr(outcome, "path", ""),
                    # The particular failure, not just its category. `reason` says
                    # `could-not-specify`; this says what could not be specified.
                    detail=str(getattr(outcome, "detail", "") or ""),
                    seconds=round(elapsed_so_far / max(1, summary.get("attempted", 0)), 3),
                    **spent))
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
        # A function corpus is served by several topics chained, so one batch draws from
        # algorithms, data structures, strings, maths, dates and more rather than from a single
        # `topic:algorithms` search.
        # A REQUESTED LANGUAGE MUST NOT BE WIDENED BACK TO PYTHON. `GitHub` appends its own
        # `language:` from the keyword argument, so naming one here too sends two of them -- and
        # GitHub reads repeated qualifiers as OR, not AND. `--scale kernel --source rust` was
        # measured returning 608 repositories: 84 Rust plus all 524 Python ones, so six candidates
        # in seven were the language the caller had explicitly not asked for. Python stays the
        # default only when nothing was requested, which is where kernel evidence exists today.
        # NO LANGUAGE MEANS NO LANGUAGE, for kernel as for every other scale.
        #
        # This used to force python whenever a kernel run named none, on the grounds that kernel
        # evidence existed only there. That stopped being true: kernel now carries attested tasks
        # in cpp, rust, go, javascript and typescript as well. What the default did instead was
        # narrow an unfiltered kernel walk to a ninth of the pond the other scales draw from -- a
        # measured batch got ONE candidate in twelve minutes while module, on the same index and
        # the same topics, got fifteen.
        #
        # The hazard the old comment describes is real but different: naming a language HERE as
        # well as on the inner `GitHub` sends two `language:` qualifiers, and GitHub reads repeats
        # as OR, so `--source rust` returned 84 Rust repositories plus all 524 Python ones. That is
        # a reason not to add a second qualifier, not a reason to invent one when the caller asked
        # for none -- `_query_for` omits the qualifier entirely when the language is empty.
        github = _chain_of_topics(source.GitHub, FUNCTION_TOPICS, language, scale="module", quota=2)
        return cls(github, scale=scale, log=lambda message: print("[source] " + message, flush=True))

    if name == "github-packages":
        if language:
            # One language, several topics chained: a package corpus is the same kind of
            # concentration problem a repo corpus is, and `topic:algorithms` alone would draw
            # every night from the same puzzle-library family.
            return cls(_chain_of_topics(source.GitHub, FUNCTION_TOPICS, language, scale="package", quota=2))
        # Open-world default: enumerate multiple language sources instead of silently restricting
        # package discovery to Python. Unsupported adapters remain explicit source rejections.
        package_languages = ("python", "javascript", "typescript", "rust", "go", "ruby", "java")
        return cls(source.Chain(
            [_chain_of_topics(source.GitHub, FUNCTION_TOPICS, item, scale="package", quota=2)
             for item in package_languages],
            name="github-packages(%s)" % "|".join(package_languages)))

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


class FormNotHonoured(RuntimeError):
    """A cross-language run was asked for and nothing downstream would have produced one.

    Separate from a refusal, which is a fact about material. This is a fact about US, and it is
    raised BEFORE any candidate is sourced so that no E2B time is spent producing the wrong thing.
    """


def _refuse_a_form_nothing_will_honour(implementation, name: str, task_form, target_language: str):
    """Fail loudly when the requested form cannot reach the emitted task.

    THE FAILURE THIS PREVENTS IS SILENT SUCCESS. `_target_language` is set on the SCALE above, and
    the only readers of that attribute live on `source/checkout.py`'s index -- a different object.
    `_index()` takes no target language and never passes one on. So a run configured with
    `form: cross` and `target_language: javascript` sourced Python, emitted Python, and reported
    `target_met: true, trustworthy: true` with `cross_language = false` in every task.toml.

    That was measured, not deduced: a module/cross python->javascript batch emitted two tasks and
    both carried `target_language = ""`. All 136 task.toml on disk before it did too, so no
    cross-language task has ever been produced -- while DESIGN.md records the form as proved.

    WHY REFUSING BEATS QUIETLY DOWNGRADING, which is the whole ethic `core/capabilities.py` is
    built on: a batch that emits the wrong form still emits, so its yield looks healthy and its
    tasks look fine one at a time. Over a long unattended run that is hundreds of same-language
    tasks filed as cross-language, and the error is only visible by reading a field nobody reads.

    The check is that the emitted spec would CARRY the language, not that some attribute was
    assigned -- assignment is exactly what already happens and exactly what does not work.
    """
    wants_cross = task_form is TaskForm.CROSS_LANGUAGE or bool(target_language)
    if not wants_cross:
        return
    if not target_language:
        raise FormNotHonoured(
            "form 'cross' needs a target_language: a cross-language task names the language the "
            "submission must be written in, and without one there is nothing to enforce.")
    # THE SCALE DECLARES IT, beside the `specify` that honours it. Not "has the attribute" --
    # `_target_language` was just assigned above and is true everywhere, which is the bug rather
    # than a test for it. `supports_cross_language` is a class attribute a scale sets only when its
    # `specify` copies the language onto the Spec, so the claim sits next to the mechanism and a
    # later edit that drops the copy has to delete a visible declaration to keep passing.
    if not getattr(implementation, "supports_cross_language", False):
        raise FormNotHonoured(
            "the %s scale does not carry target_language onto its Spec, so a run would emit "
            "same-language tasks reporting cross_language = false. Refusing rather than shipping "
            "the wrong form as though it were the right one." % name)


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
