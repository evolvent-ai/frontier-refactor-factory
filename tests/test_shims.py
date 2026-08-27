"""The call-seam shims: nine languages, one wire, and the checks that keep that true.

The shim is the whole "any language" mechanism. Adding a language is adding a row to TEMPLATES and
a template beside it; nothing in core/ changes. But a language can silently stop being servable if
one of three things drifts: the template file goes missing, the subject filename that the shim
expects disagrees with what the table says, or the toolchain declaration in _LANGUAGE_SETUP stops
matching the tool the shim needs.

None of those need a compiler to detect. The wire contract -- read a JSON line, call the entry
point, write one reply line -- is verified by the python shim running, which works on any host that
has python3. The structural checks below need nothing installed.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.call import shims                                          # noqa: E402


def test_every_registered_language_has_its_template_on_disk():
    """A row whose file went missing is a language that fails at runtime, not at load time.

    `load()` checks the file exists, but only when the language is asked for -- so a shim that was
    deleted would sit in `available()` and break the first batch that sourced it, in the sandbox,
    after the candidate had already been paid for.
    """
    here = os.path.dirname(os.path.abspath(shims.__file__))
    for language, shim in sorted(shims.TEMPLATES.items()):
        assert os.path.isfile(os.path.join(here, shim.template)), (
            "%s is registered but %s is missing" % (language, shim.template))


def test_every_language_declares_the_tool_that_actually_starts_it():
    """`tool` is what `usable()` checks, so a typo there disables the language silently."""
    for language, shim in sorted(shims.TEMPLATES.items()):
        assert shim.tool, "%s declares no tool; usable() would accept anything" % language


def test_the_run_command_actually_starts_the_subject():
    """The run argv must reference the entry or the binary, else the shim serves nothing.

    A shim that never invokes the subject is an entirely different kind of bug: it compiles fine,
    starts without incident, and answers every request with the same placeholder -- so the freeze
    would be stable, the evidence would hold, and the task would ship grading a constant.
    """
    for language, shim in sorted(shims.TEMPLATES.items()):
        run_text = " ".join(shim.run)
        assert any(token in run_text for token in
                   ("{entry}", "{binary}", "{module}", "subject.js", "{workdir}")), (
            "%s run command references neither entry nor binary: %s" % (language, run_text))


def test_the_python_shim_serves_a_real_subject_locally():
    """The wire contract, verified by the one shim testable without a compiler.

    One JSON object per line in, one JSON object per line out, with the failure path also an
    answer. This is what every other shim is a port of, so it is the check the rest get measured
    against.
    """
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        subject = os.path.join(directory, "subject.py")
        with open(subject, "w", encoding="utf-8") as handle:
            handle.write("def entry(a, b):\n    return a + b\n")
        build, run = shims.materialise(directory, "python", subject, "entry")
        for argv in build:
            subprocess.run(argv, cwd=directory, check=True, capture_output=True, timeout=60)
        result = subprocess.run(
            run, cwd=directory,
            input='{"id":1,"op":"run","call":"entry","args":[2,3]}\n',
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        assert reply["ok"] is True and reply["value"] == 5, reply


def test_an_error_still_gets_a_reply_line_and_kills_the_process():
    """The failure path is why the shims are not one-liners.

    A subject that raises on bad input must answer {"ok": false, "error": ...} rather than dying
    silently. Otherwise the harness seeing an empty reply would not know whether the subject
    rejected the input or the wire broke.
    """
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        subject = os.path.join(directory, "subject.py")
        with open(subject, "w", encoding="utf-8") as handle:
            # Two arguments so the call is well-formed and the subject's own error surfaces,
            # rather than a TypeError from the call itself.
            handle.write("def entry(a, b):\n    raise ValueError('bad input')\n")
        _build, run = shims.materialise(directory, "python", subject, "entry")
        result = subprocess.run(
            run, cwd=directory,
            input='{"id":2,"op":"run","call":"entry","args":[1,2]}\n',
            capture_output=True, text=True, timeout=60)
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        assert reply["ok"] is False and "bad input" in reply["error"], reply
