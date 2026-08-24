"""Anti-circumvention: the two defences, and the honesty of what they report.

The task is "make this faster with identical behaviour", and the shortest route to identical
behaviour is to call the original. A submission that does so is perfectly correct and has
implemented nothing, so `evidence.cannot_delegate_to_the_reference` asks whether a task defends
against it -- and this is the defence it asks about.

WHAT IS ACTUALLY BEING TESTED HERE is mostly the SECOND half. Source inspection is easy to write and
easy to check. The half that goes wrong is reporting: an `isolated()` that returns True because True
is convenient turns the evidence check into decoration, and it does so silently, in a task that
looks fully certified. Several tests below exist only to pin that down.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                            # noqa: E402

from frf.core import evidence, integrity                                 # noqa: E402


def _tree(**files) -> str:
    work = tempfile.mkdtemp(prefix="frf-integrity-")
    for name, body in files.items():
        path = os.path.join(work, name.replace("__", "."))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    return work


def test_it_catches_the_delegation_in_every_punctuation_a_language_uses():
    """`a.b`, `a::b`, `a/b` are the same delegation written three ways.

    A rule that only understood dots would let a Rust or a Node submission through by writing the
    same import with a different separator -- and that submission would pass every behavioural
    check, because it behaves exactly like the reference by construction.
    """
    tree = _tree(**{
        "a__py": "import reference.core.fast\n",
        "b__rs": "use reference::inner::go;\n",
        "c__js": "const r = require('reference/lib');\n",
        "d__c": '#include "reference.h"\n',
    })
    found = integrity.inspect(tree, ("reference",))

    assert not found.clean
    assert len(found.hits) == 4, [str(h) for h in found.hits]
    assert found.files_read == 4


def test_a_submodule_of_a_forbidden_package_is_forbidden():
    """Otherwise the ban list would have to enumerate every submodule a package might grow."""
    tree = _tree(a__py="from reference.internal.kernels import fast\n")
    assert not integrity.inspect(tree, ("reference",)).clean


def test_an_innocent_import_is_not_a_hit():
    """A checker that flagged everything would be as useless as one that flagged nothing, and it
    would be discovered later, after it had refused a batch of good material."""
    tree = _tree(a__py="import math\nfrom typing import List\nimport referenceless\n")
    assert integrity.inspect(tree, ("reference",)).clean, "prefix matching must respect boundaries"


def test_an_explicit_allowance_beats_the_ban():
    """Some tasks permit one module of an otherwise banned package. Expressing that here keeps it
    out of whichever caller would otherwise have remembered."""
    tree = _tree(a__py="import reference.types\n")
    assert not integrity.inspect(tree, ("reference",)).clean
    assert integrity.inspect(tree, ("reference",), allowed=("reference.types",)).clean


def test_run_time_naming_is_reported_separately_from_a_hit():
    """`__import__(name)` cannot be caught by reading imports, and pretending otherwise is worse
    than saying so.

    It is reported rather than ignored, and NOT counted as a hit: plenty of honest code uses these
    forms, so treating them as proof would refuse good submissions. What stops the determined case
    is that the reference is not in the image at all -- this is the second line, not the first.
    """
    tree = _tree(a__py="mod = __import__('reference')\n")
    found = integrity.inspect(tree, ("reference",))

    assert found.clean, "an indirect form is not evidence of delegation on its own"
    assert found.indirection, "but it must not pass unremarked"


def test_a_file_it_could_not_read_is_counted_rather_than_ignored():
    """"We inspected 4 files and skipped 900" is a very different claim from "we inspected it"."""
    work = _tree(a__py="import math\n")
    with open(os.path.join(work, "blob.py"), "wb") as handle:
        handle.write(b"\xff\xfe\x00binary not text\x00")

    found = integrity.inspect(work, ("reference",))
    assert found.files_read == 1 and found.files_skipped == 1
    assert found.to_json()["files_skipped"] == 1


def test_no_ban_list_means_nothing_to_find():
    """A task with no rule must not report a violation of it."""
    tree = _tree(a__py="import anything\n")
    assert integrity.inspect(tree, ()).clean


def test_isolation_is_reported_from_what_was_applied_and_never_from_a_name():
    """THE test in this file, and it once asserted the bug.

    The container is the real boundary. Applying the optional wrapper adds a process cap and account
    restriction, but is not required for remote/container isolation. The test keeps those two facts
    separate so a future wrapper change cannot accidentally redefine what E2B already guarantees.
    """
    class _Local:
        name = "local-process"

    class _Container:
        name = "docker"

    assert not integrity.isolation_for(_Local()).enforced
    assert not integrity.isolation_for(None).enforced

    # The container itself is the boundary. The wrapper is an optional further restriction,
    # not the thing that makes remote execution isolated.
    unwrapped = integrity.isolation_for(_Container())
    assert unwrapped.enforced
    assert not unwrapped.accounts
    assert unwrapped.process_cap == 0
    assert "container" in unwrapped.reason

    # The same container, with the restriction actually applied.
    wrapped = integrity.isolation_for(_Container(), applied=True)
    assert wrapped.enforced and wrapped.process_cap > 0 and wrapped.suspends_idle_side

    # Applied on a backend that shares this machine is still not separation: the account and the cap
    # are real, the isolation is not, and conflating them is how a local run gets certified.
    assert not integrity.isolation_for(_Local(), applied=True).enforced

    # The reason travels with the verdict: a caller reading provenance months later needs to know
    # WHY a check was inconclusive, not merely that it was.
    assert "clock" in integrity.isolation_for(_Local()).reason


def test_the_evidence_check_will_not_certify_half_a_defence():
    """Source inspection finding nothing is not the same as the defence being in force.

    A submission can pass inspection and still subcontract its work to a process the clock never
    sees. With only one half present the check must say so, and this is the assertion that keeps
    `cannot_delegate_to_the_reference` from becoming a rubber stamp.
    """
    both = evidence.cannot_delegate_to_the_reference(lambda: [], lambda: True)
    assert both.outcome is evidence.Outcome.HOLDS

    half = evidence.cannot_delegate_to_the_reference(lambda: [], lambda: False)
    assert half.outcome is evidence.Outcome.INCONCLUSIVE
    assert not half.ok, "a task ships on evidence, and 'we could not tell' is not any"

    caught = evidence.cannot_delegate_to_the_reference(lambda: ["a.py:1 reaches 'reference'"],
                                                       lambda: True)
    assert caught.outcome is evidence.Outcome.FAILS


def test_dropping_privileges_refuses_rather_than_silently_doing_nothing():
    """If neither wrapper is present, returning the argv unchanged would drop the defence quietly.

    That is the failure this whole module is written against: a measure that is absent but reported
    as present. Raising makes the caller decide, and `isolation_for` is what reports the outcome.
    """
    import shutil

    if shutil.which("setpriv") or shutil.which("su"):
        wrapped = integrity.restricted_argv(["/bin/echo", "hello"])
        assert wrapped[0] in ("setpriv", "su")
        assert any("ulimit" in part for part in wrapped), "the process cap must survive the wrapper"
    else:                                                    # pragma: no cover -- host-dependent
        with pytest.raises(LookupError):
            integrity.restricted_argv(["/bin/echo", "hello"])
