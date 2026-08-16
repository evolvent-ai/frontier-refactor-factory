"""The sandbox interface, and the substitution it must refuse."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import sandbox                                          # noqa: E402


def test_availability_is_measured_rather_than_assumed():
    """A caller needs to know WHY it got what it got, in a log rather than from a stack trace."""
    have = sandbox.available()
    assert set(have) >= {"docker", "remote", "local-process"}
    assert have["local-process"] is True, "this process is always available"
    assert all(isinstance(v, bool) for v in have.values())


def test_asking_for_an_unavailable_backend_fails_instead_of_substituting():
    """The substitution that must never happen silently.

    A caller that asked to freeze inside a container and was quietly given this process would emit
    an Expectation describing this host -- and nothing downstream could tell, because the recording
    looks exactly the same either way.
    """
    have = sandbox.available()
    unavailable = next((k for k in ("docker", "remote") if not have[k]), None)
    if unavailable is None:
        return                                    # both are available here; nothing to assert

    try:
        sandbox.find(prefer=unavailable)
    except sandbox.SandboxError as exc:
        assert unavailable in str(exc)
        assert "wrong machine" in str(exc), "the message says why substituting is not allowed"
    else:
        raise AssertionError("an unavailable backend must fail rather than be substituted")


def test_the_local_backend_runs_a_real_command_and_reports_honestly():
    """Named `local-process` so nobody reaches for it by accident. It is still a real runner."""
    tmp = tempfile.mkdtemp(prefix="frf-sbx-")
    try:
        backend = sandbox.LocalProcess(root=tmp)

        ok = backend.run([sys.executable, "-c", "print('hello')"])
        assert ok.ok and "hello" in ok.stdout, ok

        failed = backend.run([sys.executable, "-c", "import sys; sys.exit(3)"])
        assert not failed.ok and failed.exit_code == 3

        # A command that could not START is a different finding from one that ran and failed --
        # broken environment versus a real failure -- and callers route on the difference.
        missing = backend.run(["/definitely/not/a/program"])
        assert missing.exit_code == 127
        assert "could not execute" in missing.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_timeout_is_reported_rather_than_raised():
    """A hung build is an outcome the pipeline routes on, not an exception it dies of."""
    tmp = tempfile.mkdtemp(prefix="frf-sbx-timeout-")
    try:
        backend = sandbox.LocalProcess(root=tmp)
        slow = backend.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)
        assert not slow.ok
        assert "timed out" in slow.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_directory_goes_in_and_comes_back():
    tmp = tempfile.mkdtemp(prefix="frf-sbx-io-")
    try:
        source = os.path.join(tmp, "src")
        os.makedirs(source)
        with open(os.path.join(source, "file.txt"), "w") as fh:
            fh.write("content")

        backend = sandbox.LocalProcess(root=tmp)
        backend.push(source, os.path.join(tmp, "pushed"))
        backend.pull(os.path.join(tmp, "pushed"), os.path.join(tmp, "pulled"))
        assert open(os.path.join(tmp, "pulled", "file.txt")).read() == "content"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_failure_when_nothing_is_available_explains_the_consequence():
    """A missing sandbox is not a missing dependency -- it changes what a key would mean."""
    have = sandbox.available()
    if have["docker"] or have["remote"]:
        return

    try:
        sandbox.find()
    except sandbox.SandboxError as exc:
        assert "describe this host" in str(exc)
        assert "E2B_API_KEY" in str(exc) or "docker" in str(exc), "it says what to do about it"
    else:
        raise AssertionError("with no container backend, find() must refuse")
