"""Where host-side temporary working directories land, and what reclaims them.

This exists because of a failure that did not look like a disk problem. Checkouts, mutant build
rooms and per-scenario workspaces were staged with a bare `tempfile.mkdtemp`, which put hundreds of
megabytes each onto the root filesystem; that filled, and the batch reported
`OSError: [Errno 28] No space left on device` raised from inside a freeze -- on a graded channel,
several layers below the thing that had actually run out. Meanwhile the volume the project lives on
had hundreds of gigabytes free.

So the two properties worth a test are the two that were wrong: WHERE a directory is created, and
whether anything ever removes one that a dead run left behind.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                                            # noqa: E402

from frf.core import scratch                                             # noqa: E402


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """Point the module at a throwaway base so a test never touches the project's real one."""
    base = tmp_path / "scratch"
    monkeypatch.setenv(scratch.ENV_VAR, str(base))
    return base


def test_the_default_base_is_not_the_system_temp_directory(monkeypatch):
    """The whole point: bulk staging must not land on the filesystem that holds the OS.

    Asserted as "not under the system temp dir" rather than as a literal path, because what matters
    is the property and not the name -- an installation with no project root legitimately falls back
    there, and this test runs from a checkout that has one.
    """
    monkeypatch.delenv(scratch.ENV_VAR, raising=False)
    base = scratch.base()
    import tempfile
    system = os.path.realpath(tempfile.gettempdir())
    assert not os.path.realpath(base).startswith(system + os.sep), (
        "scratch fell back to %s, which is the filesystem that filling caused Errno 28" % system)
    # It belongs beside the project's other bulk output, which is already git-ignored.
    assert os.path.basename(base) == "scratch"
    assert os.path.basename(os.path.dirname(base)) == "work"


def test_an_exported_system_tmpdir_does_not_override_the_choice(monkeypatch, tmp_path):
    """`TMPDIR` is usually the shell's default rather than an instruction, so it must not win.

    Honouring it would silently undo the fix in the common case -- a shell that exports the system
    default -- and the failure would come back looking exactly as confusing as before.
    """
    monkeypatch.delenv(scratch.ENV_VAR, raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "somewhere-else"))
    assert not os.path.realpath(scratch.base()).startswith(os.path.realpath(str(tmp_path)))


def test_an_explicit_setting_is_honoured(elsewhere):
    """`FRF_SCRATCH_DIR` IS an instruction -- a host whose big volume is somewhere else needs it."""
    assert os.path.realpath(scratch.base()) == os.path.realpath(str(elsewhere))
    assert elsewhere.is_dir(), "the base is created rather than merely computed"


def test_a_directory_is_created_inside_the_base_and_stays_private(elsewhere):
    made = scratch.mkdtemp()
    try:
        assert os.path.dirname(os.path.realpath(made)) == os.path.realpath(str(elsewhere))
        # mkdtemp's own 0700 is right for the directory a run works in; only the shared level above
        # is opened up, and only for traversal.
        assert os.stat(made).st_mode & 0o777 == 0o700
        assert os.stat(str(elsewhere)).st_mode & 0o111, (
            "a subject running as another account has to be able to traverse into its inputs")
    finally:
        os.rmdir(made)


def test_a_leftover_is_reclaimed_once_no_live_run_could_own_it(elsewhere):
    """The accumulation half of the bug: 1627 directories had piled up because nothing swept.

    A killed batch cannot run its own teardown, so age is the only available signal for whether
    anybody still owns a directory.
    """
    stale = scratch.mkdtemp()
    with open(os.path.join(stale, "checkout.bin"), "w") as handle:
        handle.write("x" * 64)               # a leftover is a tree, not an empty directory
    old = time.time() - (scratch.DEFAULT_MAX_AGE_HOURS + 1) * 3600
    os.utime(stale, (old, old))

    assert scratch.sweep() == 1
    assert not os.path.exists(stale)


def test_a_directory_a_running_batch_might_still_hold_is_left_alone(elsewhere):
    """Sweeping is not allowed to delete the workspace of the run that is doing the sweeping."""
    live = scratch.mkdtemp()
    try:
        assert scratch.sweep() == 0
        assert os.path.isdir(live)
    finally:
        os.rmdir(live)


def test_the_sweep_only_claims_what_this_project_named(elsewhere):
    """A shared directory may hold somebody else's data, and age is not a licence to remove it."""
    stranger = os.path.join(str(elsewhere), "someone-elses-data")
    os.makedirs(stranger)
    old = time.time() - (scratch.DEFAULT_MAX_AGE_HOURS + 1) * 3600
    os.utime(stranger, (old, old))

    assert scratch.sweep() == 0
    assert os.path.isdir(stranger), "only %r-prefixed directories are ours to delete" % scratch.PREFIX
