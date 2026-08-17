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
