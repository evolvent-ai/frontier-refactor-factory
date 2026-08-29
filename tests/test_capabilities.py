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
from frf.observe.call import dispatch, servable, shims
from frf.source.function_miner import canonical, supported as minable

# The scales that reach a subject over the call seam, and therefore need both halves. `repo` is
# absent on purpose: the process seam runs a whole program, so it needs no miner and no shim.
CALL_SCALES = ("kernel", "module", "package")


def test_every_declared_call_scale_has_all_three_parts_of_the_seam():
    """A declared call scale means a miner, a shim, AND something that binds one to the other.

    THE THIRD PART IS THE ONE THAT GETS FORGOTTEN, because the other two are so visible. Counting two
    parts, go/rust/java/cpp look ready: the tree-sitter miner reads them and a shim ships for each.
    They were not. Every candidate of the first Go kernel batch died at build with
    `found packages main (serve.go) and dynamic (subject.go)`, and with the package renamed by hand,
    `undefined: Entry` underneath it -- neither message about the material.

    THE BINDING ARRIVES TWO WAYS, which is why this asks `call.servable` instead of reading a flag.
    A dynamic runtime looks the name up (serve.py, serve.js, serve.rb); a compiler needs the types
    written out, so a bridge is generated per candidate. Ruby proved the distinction is not
    "dynamic vs static": serve.rb splatted any arity but hard-coded the NAME, so a mined `two_sum`
    raised NameError until it was passed a symbol -- a shim fix, not a bridge.
    """
    missing = []
    for name, item in sorted(_REGISTRY.items()):
        declared = sorted(set(item.scales) & set(CALL_SCALES))
        if not declared or servable(name):
            continue
        if not minable(name):
            missing.append("%s declares %s but no reader can mine it" % (name, declared))
        elif name not in shims.TEMPLATES:
            missing.append("%s declares %s but ships no shim" % (name, declared))
        else:
            missing.append(
                "%s declares %s but nothing binds a mined symbol to %s: the shim does not resolve a "
                "name and no bridge is generated" % (name, declared, shims.TEMPLATES[name].template))
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

    COUNTING THREE PARTS, NOT TWO -- the first version of this test counted two, which is how it
    passed while asserting something false. A language that can be mined and has a shim looks servable
    and is not, unless something binds the mined symbol.

    The pressure runs the other way too, deliberately. When a binding lands for a language --
    serve.rb gaining a symbol argument, a bridge generator gaining an entry -- `servable` becomes true
    and this test starts demanding the registry say so. That is the direction worth being nagged in:
    recording what the factory can actually do.
    """
    understated = [
        "%s can be mined, served and bound but declares only %s" % (name, sorted(item.scales))
        for name, item in sorted(_REGISTRY.items())
        if servable(name) and not set(item.scales) & set(CALL_SCALES)]
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


def test_the_outside_worlds_spelling_is_canonicalised_before_it_is_used():
    """GitHub says `C++`; every table in this factory is keyed `cpp`.

    A REAL BATCH FAILURE. The alias table was consulted only to pick a scanner, so a C++ repository
    was mined correctly and then carried `language="c++"` onwards -- and every candidate was refused
    at specify with `no shim for 'c++'`, after the clone had been paid for. A language the factory
    fully supports, reported as one it does not, with the checkout already spent.

    Canonicalising at the boundary fixes it once for every table downstream: the shim registry, the
    capability registry, the bridge generators and the dispatchers are all keyed the same way.
    """
    for spelling in ("C++", "c++", "CPlusPlus"):
        assert canonical(spelling) == "cpp", spelling
        assert canonical(spelling) in shims.TEMPLATES
        assert capability(canonical(spelling)).level != "discovered"
    assert canonical("golang") == "go"
    assert canonical("  Rust  ") == "rust"
    # An unknown language is still reported as itself, lowercased -- not mapped to something we do
    # support, which would be far worse than not knowing it.
    assert canonical("Zig") == "zig"


def test_an_unregistered_language_stays_discovered():
    """Open-world sourcing: an unknown language is a fact to record, not an error to raise."""
    item = capability("zig", scale="repo")
    assert item.level == "discovered"
    assert item.adapter == ""
    assert item.scales == ()
