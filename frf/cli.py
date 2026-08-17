"""The command line: what this factory looks like to somebody who has not read it.

    frf scales                       what can be built, and what each needs
    frf build module --budget 20     produce tasks at one scale
    frf build all --budget 5         every registered scale
    frf doctor                       what is installed, what is missing, what that costs

Deliberately four verbs. A factory whose command line grows a flag per decision ends up encoding the
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
import sys

from . import __version__
from .core import credentials, sandbox
from .core.scale import SCALES
from .observe import coverage
from .observe.call import shims


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

    print("subjects can be served in:      %s" % ", ".join(shims.available()))
    print("line coverage can be read for:  %s" % ", ".join(coverage.available()))
    print("  (a language without a backend still ships tasks, with one quality number fewer)\n")

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
    """Run one scale, or all of them, and report the yield honestly.

    This deliberately cannot construct an index. Sourcing needs credentials and network and is
    specific to each registry, so a caller supplies it in Python; the command line is for driving a
    factory that has one, and says as much rather than pretending to be able to.
    """
    _log("the command line cannot yet construct an index for a scale.")
    _log("")
    _log("Sourcing is per-registry and needs credentials, so it is supplied in Python:")
    _log("")
    _log("    from frf import Factory")
    _log("    from frf.scales import Module")
    _log("")
    _log("    factory = Factory().register(Module(index=my_index))")
    _log("    result  = factory.build(%r, budget=%d)" % (args.scale, args.budget))
    _log("    print(result.summary())")
    _log("")
    _log("`frf scales` lists what each scale needs.")
    return 2


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
    build.add_argument("--json", action="store_true", help="print the summary as JSON")
    build.set_defaults(run=_build_command)

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
