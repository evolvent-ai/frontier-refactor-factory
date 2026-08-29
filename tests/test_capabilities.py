"""The capability registry, held against what the code can actually do.

WHY THIS FILE EXISTS. `core/capabilities.py` is a hand-written table, and nothing checked it. It
went stale twice, in both directions:

    OVERPROMISING -- go/rust/java/cpp were once declared call-capable on the strength of the shim
    table alone. A shim is half of the call seam; without a miner nothing is ever found to serve, so
    a Go module run sourced hundreds of repositories, widened them into zero candidates, and
    reported a yield for a supply that did not exist.

    UNDERSTATING -- when the tree-sitter miner landed for those same four languages, the table went
    on saying they reached the repo scale only. Nothing broke loudly, because `capability()` is read
    by attestation and audit RECORDS rather than by the pipeline. So the cells those languages could
    now produce were recorded as cells they could not, which is quieter and no less wrong.

WHAT IS AND IS NOT CHECKED HERE. A registry row makes two claims. The MECHANISM claim -- is the seam
built for this language -- is derivable from the code, and every test below checks it. The EVIDENCE
claim -- has this combination actually been graded -- is a judgement made from real batches, and no
test can derive it: `python` deliberately omits `repo` despite the process seam needing no adapter at
all, because that combination has not completed its audit. Asserting the evidence half here would
mean asserting a conclusion, so these tests only ever catch the table CONTRADICTING the code.
"""
from __future__ import annotations

from frf.core.capabilities import _REGISTRY, capability
from frf.observe.call import dispatch, shims
from frf.source.function_miner import supported as minable

# The scales that reach a subject over the call seam, and therefore need both halves. `repo` is
# absent on purpose: the process seam runs a whole program, so it needs no miner and no shim.
CALL_SCALES = ("kernel", "module", "package")


def test_every_declared_call_scale_has_all_three_parts_of_the_seam():
    """A declared call scale means a miner, a shim, AND something that binds one to the other.

    THE THIRD PART IS THE ONE THAT GETS FORGOTTEN, twice now and in the same direction. Counting two
    parts, go/rust/java/cpp look ready: the tree-sitter miner reads them and a shim ships for each.
    They are not. A dynamic shim closes the last gap at run time -- `serve.py` resolves the symbol by
    name and splats, `serve.js` indexes `subject[symbol]` -- so it serves whatever the miner found.
    A static shim cannot: `serve.go` demands `func Entry(args []interface{}) (interface{}, error)` in
    `package main`, and mined material is `func CoinChange(coins []int, amount int) int` in
    `package dynamic`. Every candidate in the first Go kernel batch died at build with
    `found packages main (serve.go) and dynamic (subject.go)`, and with the package renamed by hand,
    `undefined: Entry` underneath it.

    So `binds_symbol` is checked here rather than inferred. Ruby is the case that proves the flag is
    not a synonym for "dynamic": `serve.rb` splats any arity but hard-codes the NAME `entry` and is
    passed no symbol, so a mined `coin_change` raises NameError just the same.
    """
    missing = []
    for name, item in sorted(_REGISTRY.items()):
        declared = sorted(set(item.scales) & set(CALL_SCALES))
        if not declared:
            continue
        if not minable(name):
            missing.append("%s declares %s but no reader can mine it" % (name, declared))
        shim = shims.TEMPLATES.get(name)
        if shim is None:
            missing.append("%s declares %s but ships no shim" % (name, declared))
        elif not shim.binds_symbol:
            missing.append(
                "%s declares %s but %s cannot bind a mined symbol: it needs a generated bridge"
                % (name, declared, shim.template))
    assert not missing, missing


def test_every_language_declaring_package_has_a_dispatcher():
    """The package scale fans one entry point out to many symbols, which is per-language work.

    Separate from the check above because the package scale needs a THIRD thing the smaller call
    scales do not: a dispatcher. go/rust/java/cpp have a miner and a shim and are called capable on
    kernel and module for exactly that reason, while `package` stays absent until
    `observe/call/dispatch.py` can generate static dispatch for them.
    """
    liars = [name for name, item in sorted(_REGISTRY.items())
             if "package" in item.scales and not dispatch.supported(name)]
    assert not liars, ["%s declares the package scale with no dispatcher" % n for n in liars]


def test_a_language_the_seam_can_actually_serve_is_not_declared_repository_only():
    """The understating failure, pinned: a working seam reported as if it were not there.

    COUNTING THREE PARTS, NOT TWO -- and the first version of this test counted two, which is how it
    passed while asserting something false. A language that can be mined and has a shim looks servable
    and is not, unless that shim can bind the mined symbol; go/rust/java/cpp are exactly that case,
    and declaring them on kernel and module made the registry promise a batch that refused every
    candidate at build.

    So the condition here is the full one. When a generated bridge lands for a language, its
    `binds_symbol` becomes true and this test starts demanding the registry say so -- which is the
    intended pressure, in the direction of recording what the factory can do.
    """
    understated = []
    for name, item in sorted(_REGISTRY.items()):
        shim = shims.TEMPLATES.get(name)
        if not (minable(name) and shim is not None and shim.binds_symbol):
            continue
        if not set(item.scales) & set(CALL_SCALES):
            understated.append("%s can be mined, served and bound but declares only %s"
                               % (name, sorted(item.scales)))
    assert not understated, understated


def test_a_language_without_a_miner_declares_no_call_scale():
    """Ruby today: a shim, a package surface adapter, and no reader.

    The inverse of the test above, and it is not redundant with the first one -- that test starts
    from the declaration, this one starts from the mechanism. Ruby is the live case: it has a shim
    and an adapter, so the shim table alone would call it call-capable, and its call scales cannot
    be sourced at all because `native_functions._GRAMMARS` has no entry for it.
    """
    for name, item in sorted(_REGISTRY.items()):
        if minable(name):
            continue
        assert not set(item.scales) & set(CALL_SCALES), (
            "%s has no miner but declares %s" % (name, sorted(item.scales)))


def test_an_omitted_scale_is_reported_one_rung_down_not_as_unknown():
    """How the table says "the mechanism works, this scale has no evidence yet".

    `python` omits `repo`, and the answer must still name the registered adapter: reporting it as
    `discovered` would erase the difference between a language nobody has taught the factory and one
    whose combination is merely ungraded.
    """
    item = capability("python", scale="repo")
    assert item.level != "discovered"
    assert item.adapter == "python"


def test_an_unregistered_language_stays_discovered():
    """Open-world sourcing: an unknown language is a fact to record, not an error to raise."""
    item = capability("zig", scale="repo")
    assert item.level == "discovered"
    assert item.adapter == ""
    assert item.scales == ()
