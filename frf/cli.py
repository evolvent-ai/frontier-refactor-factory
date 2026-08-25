"""The command line: what this factory looks like to somebody who has not read it.

    frf scales                       what can be built, and what each needs
    frf build module --budget 20     produce tasks at one scale
    frf build all --budget 5         every registered scale
    frf doctor                       what is installed, what is missing, what that costs
    frf run --config config.yaml     run all jobs in a YAML config file
    frf status checkpoint.jsonl      summarise a checkpoint file
    frf validate tasks/              run harbor check on all task directories under a path
    frf sample --config c.yaml       show sample candidates without building

Deliberately few verbs. A factory whose command line grows a flag per decision ends up encoding the
decisions twice -- once in the code where they are argued for, and once here where they are not --
and the two drift. Everything that is a JUDGEMENT lives in the library; this only chooses which scale
to run and how many candidates to spend.

`doctor` exists because the commonest failure of a tool like this is environmental, and the second
commonest is a tool that reports it as a stack trace three stages later. It answers, before anything
is built: which languages can be served, which can be measured, whether a sandbox is reachable, and
whether credentials are present -- with what each absence costs, since most of them degrade rather
than block.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from . import __version__
from .core import credentials, sandbox
from .core.scale import SCALES
from . import source as indexes
from .observe import coverage


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _scales_command(_args) -> int:
    """What can be built, and what each scale needs before it can be."""
    from . import scales as implementations

    print("scales (smallest first):\n")
    described = {
        "kernel": ("one computational routine",
                   "an index of numeric routines; probes sampled from an array schema"),
        "module": ("one function or symbol",
                   "an index of functions; probes sampled from a schema"),
        "package": ("a package's whole public surface",
                    "a registry index; probes from a generator run in a container"),
        "repo": ("an entire repository",
                 "a code-search index; scenarios lifted from the project's own tests"),
    }
    for name in SCALES:
        what, needs = described[name]
        available = hasattr(implementations, name.capitalize())
        print("  %-8s %-34s %s" % (name, what, "" if available else "(not installed)"))
        print("           needs: %s\n" % needs)
    return 0


def _doctor_command(_args) -> int:
    """What is present, what is missing, and what each absence costs.

    Every line says the consequence rather than only the state. "e2b: missing" tells a reader
    nothing; "no sandbox reachable -- freezing would describe this host" tells them why they care.
    """
    print("frontier-refactor-factory %s\n" % __version__)

    print("subjects can be served in any language that compiles to a runnable process")
    print("  (process seam: all scales observe four channels, no per-language shim required)\n")

    measurable = coverage.available()
    print("line coverage has a backend:    %s" % ", ".join(measurable))
    print("  ...usable on this host:       %s"
          % (", ".join(n for n in measurable if coverage.usable(n)) or "(none)"))
    print("  (a language without a backend still ships tasks, with one quality number fewer)\n")

    print("candidates can be sourced from: %s\n" % ", ".join(indexes.available()))

    have = sandbox.available()
    for name, present in sorted(have.items()):
        print("  %-14s %s" % (name, "yes" if present else "no"))
    if not any(have.values()):
        print("\n  No sandbox is reachable. Freezing here would record what THIS machine does, and\n"
              "  the task ships an image that may not have the same toolchain -- so the expectation\n"
              "  would describe a program the solver never receives. Set E2B_API_KEY or start a\n"
              "  docker daemon.")

    print("\ncredentials:")
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "E2B_API_KEY", "GITHUB_TOKEN"):
        # PRESENCE ONLY, never the value. A diagnostic that prints a key has published it into
        # whatever log the person pastes into a bug report.
        print("  %-14s %s" % (key, "set" if credentials.get(key) else "not set"))
    return 0


def _build_command(args) -> int:
    """Run one automatically wired registry-backed scale."""
    from .automation import run
    if args.scale == "all":
        _log("build all is intentionally explicit: choose a scale so its registry query is visible")
        return 2
    report = run(args.scale, budget=args.budget, index=args.index,
                 output_dir=args.output, backend=args.backend)
    print(json.dumps(report.to_json(), sort_keys=True))
    return 0


def _run_command(args) -> int:
    """Run jobs from a YAML config or inline flags, with checkpointing and live progress."""
    from .config import RunConfig, JobConfig
    from .core.checkpoint import make_checkpoint_path

    # Build config from YAML file or inline flags.
    if args.config:
        try:
            cfg = RunConfig.from_yaml(args.config)
        except FileNotFoundError:
            _log("error: config file not found: %s" % args.config)
            return 1
        except Exception as exc:
            _log("error: could not parse config: %s" % exc)
            return 1
    else:
        # Inline flags: require at least --scale and --form.
        if not args.scale or not args.form:
            _log("error: --scale and --form are required when --config is not given")
            return 2
        cfg = RunConfig(jobs=[JobConfig(
            scale=args.scale,
            form=args.form,
            source_language=getattr(args, "source", "") or "",
            budget=args.budget,
        )])

    # Apply resume checkpoint.
    checkpoint_file = (getattr(args, "resume", None) or cfg.checkpoint_file
                       or make_checkpoint_path())

    if getattr(args, "dry_run", False):
        print("dry-run: config loaded, %d job(s)" % len(cfg.jobs))
        for job in cfg.jobs:
            print("  %s/%s lang=%s budget=%d" % (
                job.scale, job.form, job.source_language or "*", job.budget))
        print("checkpoint: %s" % checkpoint_file)
        return 0

    from .core.rate_limiter import configure as configure_limiter
    configure_limiter(
        max_concurrent=cfg.llm_max_concurrent,
        calls_per_minute=cfg.llm_calls_per_minute,
    )

    from .automation import run as auto_run, configure_e2b_slots
    configure_e2b_slots(cfg.e2b_max_active)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    backend = "remote" if cfg.sandboxed else "local-process"
    total_emitted = 0

    def _run_job(job):
        _log("[run] starting %s/%s budget=%d" % (job.scale, job.form, job.budget))
        try:
            report = auto_run(job.scale, budget=job.budget, index=job.index,
                              output_dir=cfg.output_dir,
                              backend=backend,
                              form=job.form,
                              subset=job.source_language,
                              target_language=job.target_language,
                              # Parsed from the config and previously dropped here, so a run that
                              # asked for a different number of freeze passes silently got five.
                              freeze_runs=cfg.freeze_runs,
                              ledger_file=cfg.ledger_file,
                              candidate_workers=max(1, cfg.max_concurrent // max(1, len(cfg.jobs))))
            return job, report, None
        except Exception as exc:
            return job, None, exc

    workers = min(cfg.max_concurrent, len(cfg.jobs)) if cfg.max_concurrent > 1 else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_job, job): job for job in cfg.jobs}
        for future in as_completed(futures):
            job, report, err = future.result()
            if err is not None:
                _log("[run] job %s/%s failed: %s" % (job.scale, job.form, err))
                continue
            summary = report.to_json()
            total_emitted += summary.get("emitted", 0)
            _log("[run] %s/%s: %d emitted, %.0f%% yield"
                 % (job.scale, job.form,
                    summary.get("emitted", 0),
                    100.0 * summary.get("yield_rate", 0.0)))
            print(json.dumps(summary, sort_keys=True))

    _log("[run] done: %d task(s) emitted across %d job(s)" % (total_emitted, len(cfg.jobs)))
    return 0


def _status_command(args) -> int:
    """Read a checkpoint file and print a summary table."""
    from .core.checkpoint import CheckpointReader
    path = args.checkpoint
    if not os.path.exists(path):
        _log("error: checkpoint file not found: %s" % path)
        return 1
    reader = CheckpointReader(path)
    summary = reader.summary()
    print("Checkpoint: %s" % path)
    print("Total records: %d" % summary["total"])
    print()
    if summary["by_status"]:
        print("By status:")
        for status, count in sorted(summary["by_status"].items()):
            print("  %-20s %d" % (status, count))
    if summary["by_scale"]:
        print()
        print("By scale:")
        for scale, count in sorted(summary["by_scale"].items()):
            print("  %-20s %d" % (scale, count))
    if summary["by_stage"]:
        print()
        print("Refusals/errors by stage:")
        for stage, count in sorted(summary["by_stage"].items(), key=lambda kv: -kv[1]):
            print("  %-30s %d" % (stage, count))
    if summary["emitted_paths"]:
        print()
        print("Emitted tasks (%d):" % len(summary["emitted_paths"]))
        for p in summary["emitted_paths"][:20]:
            print("  %s" % p)
        if len(summary["emitted_paths"]) > 20:
            print("  ... and %d more" % (len(summary["emitted_paths"]) - 20))
    return 0


def _validate_command(args) -> int:
    """Run harbor check on all task directories under a path."""
    import subprocess
    root = args.path
    if not os.path.isdir(root):
        _log("error: not a directory: %s" % root)
        return 1

    # Walk looking for directories that contain a harbor.toml or Dockerfile,
    # which marks them as task packages.
    task_dirs = []
    for entry in sorted(os.listdir(root)):
        candidate = os.path.join(root, entry)
        if not os.path.isdir(candidate):
            continue
        if (os.path.exists(os.path.join(candidate, "harbor.toml"))
                or os.path.exists(os.path.join(candidate, "Dockerfile"))):
            task_dirs.append(candidate)

    if not task_dirs:
        _log("no task directories found under %s" % root)
        return 0

    passed = 0
    failed = 0
    for td in task_dirs:
        result = subprocess.run(["harbor", "check", td],
                                capture_output=True, text=True, timeout=300)
        status = "PASS" if result.returncode == 0 else "FAIL"
        print("%-6s %s" % (status, td))
        if result.returncode == 0:
            passed += 1
        else:
            failed += 1
            if result.stdout.strip():
                print("       " + result.stdout.strip()[:200])

    print()
    print("%d passed, %d failed" % (passed, failed))
    return 0 if failed == 0 else 1


def _sample_command(args) -> int:
    """Show sample candidates without building."""
    from .config import RunConfig, JobConfig

    if args.config:
        try:
            cfg = RunConfig.from_yaml(args.config)
        except Exception as exc:
            _log("error: could not parse config: %s" % exc)
            return 1
    else:
        if not args.scale or not args.form:
            _log("error: --scale and --form are required when --config is not given")
            return 2
        cfg = RunConfig(jobs=[JobConfig(
            scale=args.scale,
            form=getattr(args, "form", "inplace"),
            budget=args.count,
        )])

    count = args.count
    from . import scales as scale_impls
    from .automation import _index

    for job in cfg.jobs:
        scale_name = job.scale
        impl = getattr(scale_impls, scale_name.capitalize(), None)
        if impl is None:
            _log("scale %r not available; skipping" % scale_name)
            continue
        try:
            source_name = {
                "module": "github-functions", "kernel": "github-functions",
                "package": "github-packages", "repo": "github",
            }.get(scale_name, "")
            index = _index(source_name, subset=job.source_language, scale=scale_name)
            instance = impl(index=index)
        except Exception as exc:
            _log("could not instantiate scale %r: %s" % (scale_name, exc))
            continue
        print("--- %s (showing up to %d) ---" % (scale_name, count))
        try:
            for i, candidate in enumerate(instance.find(count)):
                if i >= count:
                    break
                print("  %s  [%s]" % (candidate.identity, candidate.language))
        except Exception as exc:
            _log("find() failed: %s" % exc)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frf",
        description="Turn real code into performance-oriented refactoring tasks.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="frf %s" % __version__)
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("scales", help="what can be built, and what each needs")
    listing.set_defaults(run=_scales_command)

    doctor = commands.add_parser("doctor", help="what is installed and what each gap costs")
    doctor.set_defaults(run=_doctor_command)

    build = commands.add_parser("build", help="produce tasks at one scale")
    build.add_argument("scale", choices=list(SCALES) + ["all"])
    build.add_argument("--budget", type=int, default=1, help="how many candidates to try")
    build.add_argument("--output", default="tasks", help="where task packages are written")
    build.add_argument("--index", help="registry/index name (defaults by scale)")
    build.add_argument("--backend", default="remote", choices=("remote", "docker"),
                       help="sandbox backend (default: remote)")
    build.add_argument("--json", action="store_true", help="print the summary as JSON")
    build.set_defaults(run=_build_command)

    # --- frf run ---
    run_cmd = commands.add_parser(
        "run", help="run all jobs from a YAML config (or inline flags)")
    run_cmd.add_argument("--config", default="", metavar="CONFIG_YAML",
                         help="path to a YAML job config file")
    run_cmd.add_argument("--scale", default="", choices=list(SCALES) + [""],
                         help="scale (when --config is not given)")
    run_cmd.add_argument("--form", default="", choices=("inplace", "cross", ""),
                         help="form: inplace or cross (when --config is not given)")
    run_cmd.add_argument("--source", default="", metavar="LANGUAGE",
                         help="source language filter")
    run_cmd.add_argument("--budget", type=int, default=10,
                         help="candidates to try (when --config is not given)")
    run_cmd.add_argument("--resume", default="", metavar="CHECKPOINT_JSONL",
                         help="path to an existing checkpoint file to resume from")
    run_cmd.add_argument("--dry-run", action="store_true",
                         help="print the resolved config without building")
    run_cmd.set_defaults(run=_run_command)

    # --- frf status ---
    status_cmd = commands.add_parser("status", help="summarise a checkpoint file")
    status_cmd.add_argument("checkpoint", metavar="CHECKPOINT_JSONL",
                            help="path to a .jsonl checkpoint file")
    status_cmd.set_defaults(run=_status_command)

    # --- frf validate ---
    validate_cmd = commands.add_parser(
        "validate", help="run harbor check on all task directories under a path")
    validate_cmd.add_argument("path", metavar="TASKS_DIR",
                              help="directory containing task packages")
    validate_cmd.set_defaults(run=_validate_command)

    # --- frf sample ---
    sample_cmd = commands.add_parser(
        "sample", help="show sample candidates without building")
    sample_cmd.add_argument("--config", default="", metavar="CONFIG_YAML",
                            help="path to a YAML job config file")
    sample_cmd.add_argument("--scale", default="", choices=list(SCALES) + [""],
                            help="scale (when --config is not given)")
    sample_cmd.add_argument("--form", default="inplace", choices=("inplace", "cross"),
                            help="form (when --config is not given)")
    sample_cmd.add_argument("--count", type=int, default=3,
                            help="how many candidates to show per scale (default: 3)")
    sample_cmd.set_defaults(run=_sample_command)

    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except KeyboardInterrupt:
        _log("interrupted")
        return 130
    except (LookupError, ValueError, RuntimeError) as exc:
        # The errors this library raises deliberately, with messages written to be read. A traceback
        # here would bury the sentence that was composed to explain the problem.
        _log("error: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
