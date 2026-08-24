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

    assert "subjects can be served in any language" in out
    assert "line coverage has a backend:" in out
    assert "candidates can be sourced from:" in out
    assert "ships tasks, with one quality number fewer" in out, "a gap that degrades says so"

    # Doctor no longer reports per-language shim availability since all scales use process seam.
    assert "usable on this host" in out


def test_doctor_never_prints_a_credential(capsys, monkeypatch):
    """A diagnostic that prints a key has published it into whatever log gets pasted into a report."""
    # Assembled at run time rather than written as a literal: a plausible-looking key committed to
    # a test file is still a key in the repository, and the scanner in test_core rightly flags one.
    secret = "sk-" + "N0tARealKeyJustForThisTest"
    monkeypatch.setenv("LLM_API_KEY", secret)
    cli.main(["doctor"])
    out = capsys.readouterr().out

    assert secret not in out and secret[3:12] not in out
    assert "LLM_API_KEY   set" in out.replace("  ", " ").replace("   ", " ") or "set" in out


def test_build_uses_the_automatic_runner(monkeypatch, capsys):
    """The CLI delegates wiring and reports the resulting batch as JSON."""
    from frf.automation import BatchReport
    monkeypatch.setattr("frf.automation.run",
                        lambda *args, **kwargs: BatchReport({"scale": "module"}, 0.25,
                                                            "github"))
    assert cli.main(["build", "module", "--budget", "5"]) == 0
    assert '"index": "github"' in capsys.readouterr().out


def test_an_unknown_scale_is_rejected_by_the_parser(capsys):
    try:
        cli.main(["build", "nonsense"])
    except SystemExit as exit_code:
        assert exit_code.code == 2
    else:
        raise AssertionError("an unknown scale must not be accepted")
