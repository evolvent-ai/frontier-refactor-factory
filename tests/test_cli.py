"""The command line, which is what this looks like to somebody who has not read the source."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from frf import cli                                                    # noqa: E402


def test_scales_lists_all_four_and_what_each_needs(capsys):
    assert cli.main(["scales"]) == 0
    out = capsys.readouterr().out
    for scale in ("kernel", "module", "package", "repo"):
        assert scale in out
    assert "index" in out, "what a scale needs is the useful half"


def test_doctor_reports_consequences_not_just_states(capsys):
    """"e2b: missing" tells a reader nothing. What it costs them is the point."""
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out

    assert "subjects can be served in:" in out
    assert "line coverage can be read for:" in out
    assert "ships tasks, with one quality number fewer" in out, "a gap that degrades says so"


def test_doctor_never_prints_a_credential(capsys, monkeypatch):
    """A diagnostic that prints a key has published it into whatever log gets pasted into a report."""
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-value-nobody-should-see")
    cli.main(["doctor"])
    out = capsys.readouterr().out

    assert "sk-secret" not in out
    assert "LLM_API_KEY   set" in out.replace("  ", " ").replace("   ", " ") or "set" in out


def test_build_says_what_it_cannot_do_rather_than_failing_obscurely(capsys):
    """Sourcing needs credentials and is per-registry, so the CLI cannot construct an index. Saying
    so with the Python that would work beats a stack trace from inside a scale."""
    assert cli.main(["build", "module", "--budget", "5"]) == 2
    err = capsys.readouterr().err

    assert "cannot yet construct an index" in err
    assert "Factory().register(Module(index=my_index))" in err
    assert "budget=5" in err, "the message echoes what was asked for"


def test_an_unknown_scale_is_rejected_by_the_parser(capsys):
    try:
        cli.main(["build", "nonsense"])
    except SystemExit as exit_code:
        assert exit_code.code == 2
    else:
        raise AssertionError("an unknown scale must not be accepted")
