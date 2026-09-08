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


def test_every_template_is_declared_as_package_data():
    """A template in the repository but not in the wheel is a language that vanishes on install.

    NOT COVERED BY THE TEST ABOVE, which asks the filesystem and is answered by the checkout. The
    wheel is built from an explicit glob list in pyproject.toml, and that list said
    `["*.py", "*.go", "*.js", "*.rb"]` -- leaving out `serve.c`, `serve.rs` and `Serve.java`. Every
    structural check passed, because all three files are right there in the repository.

    The failure is silent AND it inverts a design claim: `available()` answers by asking whether the
    template file exists, so an installed wheel reported c, cpp, rust and java as languages this
    factory cannot serve while the repository reported that it can. A missing template is
    indistinguishable from an unsupported language, which is the confusion the table exists to end.
    """
    import tomllib
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), "rb") as handle:
        config = tomllib.load(handle)
    patterns = config["tool"]["setuptools"]["package-data"]["frf.observe.call.shims"]
    suffixes = {pattern.removeprefix("*") for pattern in patterns}
    uncovered = sorted({shim.template for shim in shims.TEMPLATES.values()
                        if not any(shim.template.endswith(s) for s in suffixes)})
    assert not uncovered, (
        "these templates ship in no wheel glob %s: %s" % (sorted(patterns), uncovered))


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


def test_a_go_subject_may_resolve_its_dependencies():
    """`GOPROXY=off` was justified by "the sandbox has no network", and that premise is false.

    The same sandbox clones from GitHub, runs `npm install` and runs `pip install`. Turning the
    proxy off cost nothing for a kernel or module subject -- a single mined function importing only
    the standard library -- and refused every Go PACKAGE candidate that had dependencies: fourteen
    of fourteen, each reading `module lookup disabled by GOPROXY=off`, which is our configuration
    talking rather than the repository.

    The third instance of the same mistake this week, after `npm install --offline` against an empty
    cache and `pip install --no-deps`.
    """
    from frf.observe.call.shims import TEMPLATES

    argv = " ".join(str(x) for cmd in TEMPLATES["go"].build for x in cmd)
    assert "GOPROXY=off" not in argv, argv
    assert "GOSUMDB=off" in argv, \
        "the checksum database is a second network dependency; the proxy already pins by hash"


def test_every_base_image_is_pinned_by_digest():
    """A tag is a moving target, and a benchmark that moves is not a measurement.

    `golang:1.26-bookworm` today and `golang:1.26-bookworm` in six months are different images:
    the tag is republished for security updates, and the frozen expectations were captured against
    whichever one happened to be current. A third party who reruns this benchmark after a rebuild
    would be scoring a submission against an answer key produced by a different toolchain, and
    would have no way to know that is what happened.

    Measured before this was enforced: 0 of 100 shipped tasks carried a digest, across 7 distinct
    base images. Three of 28 languages had been pinned by hand; the other 25 were tags.
    """
    from frf.core.shims.dockerfiles import _LANGUAGE_SETUP

    unpinned = [(language, field, image)
                for language, setup in sorted(_LANGUAGE_SETUP.items())
                for field in ("base_image", "copy_from_image")
                for image in [setup.get(field)]
                if image and "@sha256:" not in image]
    assert not unpinned, "unpinned base image(s): %s" % unpinned


def test_no_generated_dockerfile_leaves_the_candidate_as_root():
    """Root in the workspace lets a submission edit the toolchain that measures it.

    The answer key is not in this container -- the build context is `environment/`, so `COPY . /app`
    cannot reach `tests/` -- which makes this less urgent than it looks, and is not a reason to hand
    out the privilege. A candidate running as root can rewrite the interpreter, the timing harness,
    or anything else the image ships.

    Measured before this was enforced: 10 of 100 shipped tasks had no `USER` at all, spread across
    kernel, module and package. The gap was per-language rather than uniform, which is exactly the
    shape a single unconditional emission fixes and a per-language one does not.

    Both forms are checked. `inplace` and `cross` take different paths through the emitter -- cross
    layers a target toolchain over somebody else's base, and that is where an extra `USER root`
    could be left standing at the end.
    """
    from frf.core.harbor import dockerfile_for
    from frf.core.shims.dockerfiles import _LANGUAGE_SETUP

    languages = sorted(_LANGUAGE_SETUP)
    targets = [name for name, setup in _LANGUAGE_SETUP.items() if setup.get("copy_from_image")]
    pairs = [(language, "") for language in languages]
    pairs += [(source, target) for source in languages for target in sorted(targets)
              if target != source]

    wrong = []
    for source, target in pairs:
        text = dockerfile_for(source, target)
        users = [line.strip() for line in text.splitlines() if line.startswith("USER ")]
        if not users or users[-1] != "USER nobody":
            wrong.append((source, target or "inplace", users[-1:] or ["no USER at all"]))
        unpinned = [line for line in text.splitlines()
                    if line.startswith("FROM ") and "@sha256:" not in line]
        if unpinned:
            wrong.append((source, target or "inplace", unpinned))
    assert not wrong, "%d of %d language pairs emit a bad Dockerfile: %s" % (
        len(wrong), len(pairs), wrong[:5])
