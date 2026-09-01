"""The container backends, and the parts of them that can be checked without a daemon.

Most of what a sandbox does needs a sandbox, and there is no honest way to test that without one --
a mocked container tests the mock. So this file tests the two things that are real code either way:
the tar plumbing both backends share, and the refusal to substitute one backend for another.

WHY THE TAR PLUMBING GETS THIS MUCH ATTENTION. It is the only part of a sandbox that handles bytes
this process did not produce. A stream coming back out of a container was written by code we did not
write, and a member named `../../etc/something` is exactly what an escape attempt looks like. It is
also where a subtle correctness bug lives: a push that is not byte-identical for an identical tree
defeats every cache above it and makes two builds of the same subject look different.
"""
from __future__ import annotations

import io
import os
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                            # noqa: E402

from frf.core import containers, sandbox                                 # noqa: E402


def _tree() -> str:
    work = tempfile.mkdtemp(prefix="frf-containers-")
    os.makedirs(os.path.join(work, "sub", ".git"))
    with open(os.path.join(work, "a.txt"), "w") as handle:
        handle.write("hello")
    with open(os.path.join(work, "sub", "b.txt"), "w") as handle:
        handle.write("world")
    with open(os.path.join(work, "sub", ".git", "HEAD"), "w") as handle:
        handle.write("ref: refs/heads/main")
    return work


def test_a_push_leaves_the_host_out_of_the_archive():
    """Version control metadata and build caches carry absolute paths from the machine that made
    them, which is the kind of host detail that must never reach an expectation."""
    packed = containers._tar_bytes(_tree())
    names = tarfile.open(fileobj=io.BytesIO(packed)).getnames()

    assert "a.txt" in names and "sub/b.txt" in names
    assert not any(".git" in name for name in names), names
    assert not any(name.startswith("/") for name in names), "paths must be relative to the root"


def test_packing_the_same_tree_twice_produces_the_same_bytes():
    """Otherwise every push looks like a change, and no layer above this can be cached.

    Achieved by flattening mtimes and ownership: the file contents are what identifies a tree, and
    the timestamp is a property of when it happened to be written.
    """
    work = _tree()
    assert containers._tar_bytes(work) == containers._tar_bytes(work)


def test_a_pull_refuses_a_member_that_would_write_outside_the_destination():
    """A stream coming back from a sandbox was written by code we did not write.

    This is not a hypothetical about hostile registries: it is the shape an escape from the
    measurement would take, and the extraction is the only place it can be refused.
    """
    source = _tree()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        path = os.path.join(source, "a.txt")
        info = archive.gettarinfo(path, arcname="../escaped.txt")
        with open(path, "rb") as handle:
            archive.addfile(info, handle)

    destination = tempfile.mkdtemp(prefix="frf-pull-")
    containers._untar_bytes(buffer.getvalue(), destination)

    outside = os.path.join(os.path.dirname(destination), "escaped.txt")
    assert not os.path.exists(outside), "a member escaped the destination directory"


def test_a_tree_survives_the_round_trip_it_is_packed_for():
    packed = containers._tar_bytes(_tree())
    destination = tempfile.mkdtemp(prefix="frf-round-")
    containers._untar_bytes(packed, destination)

    assert open(os.path.join(destination, "a.txt")).read() == "hello"
    assert open(os.path.join(destination, "sub", "b.txt")).read() == "world"


def test_asking_for_a_backend_that_is_absent_refuses_rather_than_substituting():
    """THE contract of `find`. A key frozen somewhere other than where it was asked for describes
    the wrong machine, and the caller has no way to discover that it happened."""
    have = sandbox.available()
    missing = next((name for name, present in have.items() if not present), "")
    if not missing:                                          # pragma: no cover -- host-dependent
        pytest.skip("every backend is available here, so substitution cannot be provoked")

    with pytest.raises(sandbox.SandboxError) as caught:
        sandbox.find(missing)
    assert "substitute" in str(caught.value)


def test_docker_is_probed_by_asking_the_daemon_not_by_looking_at_path():
    """`docker` on PATH inside a container with no socket is the commonest false positive there is.

    Believing PATH turns "there is no sandbox here" into an expensive failure at freeze time rather
    than a clear one before any work begins.
    """
    import shutil

    reported = sandbox.available()["docker"]
    if shutil.which("docker") and not reported:
        return                                    # the CLI is here and the daemon is not: correct
    assert reported == containers.docker_available()


def test_the_local_backend_is_named_so_that_nobody_reaches_for_it_by_accident():
    """It isolates nothing, so a key frozen under it describes this host. The name is the warning,
    and `integrity.isolation_for` is what turns that into a verdict."""
    from frf.core import integrity

    assert sandbox.LocalProcess.name == "local-process"
    assert not integrity.isolation_for(sandbox.LocalProcess(root="/tmp")).enforced


def test_a_remote_sandbox_without_a_key_says_which_key():
    """A diagnostic that names the missing credential is the whole of the user's fix.

    It must not name the VALUE, which is why `credentials` is the only reader -- see its docstring.
    """
    from frf.core import credentials

    if credentials.get("E2B_API_KEY"):                       # pragma: no cover -- host-dependent
        pytest.skip("a key is set here, so its absence cannot be provoked")
    with pytest.raises(sandbox.SandboxError) as caught:
        containers.Remote()
    message = str(caught.value)
    assert "E2B_API_KEY" in message or "e2b" in message


def test_every_call_to_the_remote_api_is_bounded_on_the_wire():
    """An unbounded remote call does not fail -- it waits, which is worse than failing.

    THE E2B SDK TAKES TWO TIMEOUTS AND THEY ARE EASY TO CONFLATE. `timeout` bounds the process inside
    the sandbox; `request_timeout` bounds the HTTP call carrying it, and defaults to None, meaning wait
    forever. We passed only the first.

    A kernel/java batch paid for that: 28 minutes with its main thread in futex_wait and an ESTAB
    socket to the API, having widened its candidates and never reached a build. It also made the retry
    logic in `run` unreachable for the failure it was written for -- a call that never returns never
    raises, so the `timed out` match below it never had anything to match.

    CHECKED AT SOURCE LEVEL, deliberately. This file's preamble says a mocked container tests the mock,
    and there is nothing to assert at runtime: the bug's whole signature is a call that does not come
    back. Reading the call sites is what actually catches the omission, and it catches it for a call
    site added later, which is the case that matters.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(containers))
    remote = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.ClassDef) and node.name == "Remote")

    # The SDK entry points that cross the network. `Sandbox.create` is spelled as an attribute call on
    # a name, the rest hang off `self._sandbox`.
    #
    # `kill` IS IN THIS SET BECAUSE IT WAS LEFT OUT ONCE, and the omission cost a second batch after
    # the first four call sites were fixed: a kernel/java run finished its work and then sat in
    # teardown for eleven minutes with a live socket to the API. Its `except Exception` looked like
    # cover and was not -- a call that never returns never raises. TEARDOWN counts as crossing the
    # network, so it belongs here; the lesson is that this set must name every SDK method, not the
    # ones that felt important.
    wanted = {"create", "run", "write", "read", "kill"}
    unbounded = []
    for node in ast.walk(remote):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in wanted:
            continue
        source = ast.unparse(node.func)
        if "Sandbox" not in source and "_sandbox" not in source:
            continue                                    # a local helper of the same name
        if not any(keyword.arg == "request_timeout" for keyword in node.keywords):
            unbounded.append(ast.unparse(node)[:70])
    assert not unbounded, (
        "these remote calls can wait forever, which stalls a batch instead of failing it: %s"
        % unbounded)


def test_a_sandbox_outlives_the_longest_stage_that_runs_inside_it():
    """A container's lifetime must EXCEED the work it is asked to hold, not equal it.

    `Remote.__init__` defaulted to 3600 seconds -- the same number as `FRF_FREEZE_MAX_SECONDS`. So a
    freeze permitted to take its full hour was racing the sandbox holding it, and the sandbox won:
    `The sandbox was not found: This error is likely due to sandbox timeout`, after 3616 seconds, on
    the one Go candidate that reached freeze in a real batch.

    The package scale is where an equality like this turns into a failure, because compiling the
    subject inside the sandbox -- a step this scale only recently started doing at all -- puts real
    minutes into each of the five freeze runs.

    ASSERTED AS AN ORDERING, not as two constants. Pinning the literals would pass while somebody
    raised the freeze budget to meet the lifetime, which is the same bug with different numbers.
    """
    import inspect
    import os

    from frf.observe.call import stages

    freeze_budget = float(os.environ.get("FRF_FREEZE_MAX_SECONDS", "3000"))
    assert containers.SANDBOX_LIFETIME > freeze_budget, (
        "a sandbox that expires at or before its freeze budget kills the freeze it is holding: "
        "lifetime %s, freeze %s" % (containers.SANDBOX_LIFETIME, freeze_budget))
    # The freeze reads that same environment variable, so the two figures are about one clock.
    assert "FRF_FREEZE_MAX_SECONDS" in inspect.getsource(stages.freeze)



def test_a_stalled_sandbox_command_is_killed_rather_than_waited_on_for_ever():
    """`CommandHandle.wait()` takes no timeout -- only callbacks -- and iterates until the far side
    ends the stream.

    The sandbox's own `timeout` normally ends it, by killing the command so the stream closes, and
    that is the first line of defence. This is the second: a stream that stalls without the command
    dying returns never, and a batch stops with no output, no error, and nothing naming the
    candidate that did it. `TRANSPORT_HEADROOM` exists because a call that never returns also never
    raises; this is that lesson one layer further in.
    """
    import time

    class Stalled:
        killed = False

        def wait(self):
            time.sleep(30)                      # longer than any deadline this test sets

        def kill(self):
            Stalled.killed = True
            return True

    started = time.monotonic()
    with pytest.raises(containers._CommandTimedOut, match="was killed"):
        containers._wait_bounded(Stalled(), 1.0)
    assert time.monotonic() - started < 10, "the deadline did not fire"
    assert Stalled.killed, "a command left running holds sandbox resources nobody will read"


def test_the_bounded_wait_returns_the_result_and_not_the_handle():
    """A CommandHandle carries no stdout, stderr or exit_code -- CommandResult carries all three.

    Returning the handle would leave every caller reading "" out of `getattr` and reporting a
    silent success for commands that printed, failed, or both.
    """
    class Finished:
        def wait(self):
            return _FakeResult()

        def kill(self):
            raise AssertionError("a command that finished must not be killed")

    class _FakeResult:
        exit_code = 3
        stdout = "out"
        stderr = "err"

    result = containers._wait_bounded(Finished(), 5.0)
    assert result.exit_code == 3 and result.stdout == "out" and result.stderr == "err"


def test_a_non_zero_exit_still_reaches_the_caller_as_an_exception():
    """The foreground path raised CommandExitException and `run` translated it into a Result.

    Backgrounding must not change that: the worker's exception has to re-raise here, or a failing
    build would arrive looking like a success with an empty message.
    """
    class Failing:
        def wait(self):
            raise RuntimeError("CommandExitException-ish")

        def kill(self):
            raise AssertionError("a command that already failed must not be killed")

    with pytest.raises(RuntimeError, match="CommandExitException-ish"):
        containers._wait_bounded(Failing(), 5.0)


def test_a_sandbox_lifetime_stays_inside_what_the_api_accepts():
    """E2B answers `400: Timeout cannot be greater than 1 hours`.

    A longer lifetime is not a longer sandbox, it is no sandbox. The default was 5400 and every
    creation that used it was refused -- twelve in one repo batch, none reaching the ledger, because
    a sandbox that never opened is not a candidate that failed.
    """
    import importlib
    import os

    from frf.core import containers

    assert containers.SANDBOX_LIFETIME <= containers.SANDBOX_LIFETIME_CEILING

    os.environ["FRF_SANDBOX_LIFETIME"] = "99999"
    try:
        reloaded = importlib.reload(containers)
        assert reloaded.SANDBOX_LIFETIME == reloaded.SANDBOX_LIFETIME_CEILING, \
            "an environment asking for more must get the most it can have, not nothing"
    finally:
        del os.environ["FRF_SANDBOX_LIFETIME"]
        importlib.reload(containers)
