"""The remote sandbox, against the real thing.

Everything else about a sandbox can be checked without one -- the tar plumbing, the refusal to
substitute a backend -- and `test_containers.py` does that. This file is the part that cannot: a
backend is a set of claims about someone else's API, and a mocked sandbox tests the mock.

WHY THIS FILE EXISTS AT ALL, given the cost of a live test. Every bug found while writing this
backend was an API mismatch that no amount of local reasoning would have caught, and each would have
surfaced at the most expensive possible moment -- during a freeze, after a build:

    `Sandbox(template=..., api_key=...)` is not how the 2.x SDK constructs a sandbox. The
    constructor takes an internal options object; `Sandbox.create` is the entry point.

    A NON-ZERO EXIT RAISES. `commands.run("false")` throws `CommandExitException` rather than
    returning a result with exit code 1 -- so the ordinary case of "the build failed, read the
    message" arrived as an exception, and `Result.ok` was unreachable on this backend.

    `files.read(..., format="bytes")` returns a bytearray, which `tarfile` will not accept.

None of those is deducible from a signature, and all three are load-bearing.

SKIPPED WITHOUT A KEY, NEVER FAILED. A machine with no `E2B_API_KEY` cannot run these and that is
not a defect in the code under test. What is NOT tolerated is pretending: the skip says which
credential is missing, so a suite that quietly stopped covering the remote backend looks different
from one that never could.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                            # noqa: E402

from frf.core import credentials, sandbox                                 # noqa: E402

# Opening a sandbox takes seconds and costs money, so the whole module shares one. That is a
# deliberate trade against the usual preference for isolation between tests: the alternative is
# eight sandboxes for eight assertions about the same object.
pytestmark = pytest.mark.skipif(
    not credentials.get("E2B_API_KEY"),
    reason="E2B_API_KEY is not set, so no remote sandbox can be opened")


@pytest.fixture(scope="module")
def remote():
    back = sandbox.find(prefer="remote")
    try:
        yield back
    finally:
        back.close()


def test_the_preferred_backend_is_the_remote_one(remote):
    """The all-remote decision, checked rather than assumed.

    A local daemon is faster and free, and preferring it is the obvious thing to do -- which is why
    this is a test. An Expectation frozen against whichever machine the factory happened to run on
    is not portable, and the difference shows up later as material that mysteriously stopped
    reproducing on a colleague's checkout.
    """
    assert remote.name == "remote"
    assert sandbox.available()["remote"] is True


def test_a_command_runs_and_reports_its_streams(remote):
    done = remote.run(["sh", "-c", "echo out; echo err >&2"])
    assert done.ok and done.exit_code == 0
    assert "out" in done.stdout and "err" in done.stderr


def test_a_failed_command_is_a_result_and_not_an_exception(remote):
    """The finding that mattered most. This SDK raises for any non-zero exit.

    Left alone, a build failure -- the single most common thing a sandbox reports -- would propagate
    as `CommandExitException` from whichever stage happened to call it, and `Result.ok` would be
    dead code on this backend. Every caller would then need a second code path for the remote case,
    which is exactly what the Backend protocol exists to prevent.
    """
    done = remote.run(["sh", "-c", "echo why >&2; exit 7"])
    assert done.exit_code == 7, done
    assert not done.ok
    assert "why" in done.stderr
    assert "why" in done.tail()


def test_a_directory_goes_in_and_comes_back_with_what_the_sandbox_made(remote):
    """Push, run, pull -- the whole of what the pipeline asks a sandbox for.

    Nesting is included because the tar path is where it would be lost, and a subject is never one
    flat directory.
    """
    local = tempfile.mkdtemp(prefix="frf-e2b-push-")
    os.makedirs(os.path.join(local, "pkg", "inner"))
    with open(os.path.join(local, "pkg", "inner", "value.txt"), "w") as handle:
        handle.write("nested\n")
    with open(os.path.join(local, "run.py"), "w") as handle:
        handle.write("open('made.txt','w').write('by the sandbox')\n")

    room = "/home/user/frf-probe"
    remote.push(local, room)

    listed = remote.run(["sh", "-c", "cat pkg/inner/value.txt"], workdir=room)
    assert listed.ok and "nested" in listed.stdout, listed

    built = remote.run(["python3", "run.py"], workdir=room)
    assert built.ok, built.tail()

    back = tempfile.mkdtemp(prefix="frf-e2b-pull-")
    remote.pull(room, back)
    assert os.path.exists(os.path.join(back, "pkg", "inner", "value.txt"))
    with open(os.path.join(back, "made.txt")) as handle:
        assert handle.read().strip() == "by the sandbox"


def test_credentials_reach_the_sandbox_as_environment_and_not_as_a_file(remote):
    """A pushed secret lands on a disk this process does not own and can travel home in an artefact.

    So the interface takes `env`, and this checks that it genuinely arrives -- an env argument that
    were silently dropped would send every stage that needs a gateway key into an authentication
    failure, diagnosed as a bad key rather than as a lost variable.
    """
    seen = remote.run(["sh", "-c", "echo $FRF_TEST_VALUE"], env={"FRF_TEST_VALUE": "arrived"})
    assert seen.stdout.strip() == "arrived", seen

    # And it does not outlive the command that carried it.
    after = remote.run(["sh", "-c", "echo [$FRF_TEST_VALUE]"])
    assert after.stdout.strip() == "[]", after


def test_the_sandbox_has_room_to_build_something_real(remote):
    """The template is part of what this factory is, so its shape is asserted.

    A default sandbox has under 512 MiB, which is below what a scientific wheel needs to install --
    measured on the previous factory, where it showed up as `rc=-9` from an OOM kill and looked for
    a while like a flaky build. The roomier template is the fix, and a run that silently fell back
    to the default would reintroduce it.
    """
    memory = remote.run(["sh", "-c", "free -m | awk '/^Mem:/ {print $2}'"])
    assert memory.ok, memory
    assert int(memory.stdout.strip()) >= 2000, (
        "this sandbox has %s MiB, which is not enough to install a scientific package; check "
        "E2B_TEMPLATE" % memory.stdout.strip())

    cores = remote.run(["nproc"])
    assert int(cores.stdout.strip()) >= 2, cores
