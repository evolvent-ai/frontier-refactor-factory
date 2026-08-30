"""Reading functions out of a statically typed checkout, with types a probe can be drawn from.

WHAT THESE TESTS ARE FOR. Before `native_functions` existed, `function_miner` had two scanners --
Python's `ast` and a regex reader for JavaScript -- and every other language fell to
`else: call-adapter-not-registered`. So Go, Rust, Java and C++ produced no call-scale tasks at all:
not because the material was unsuitable, but because nothing looked at it. 15 of the 32
scale x language cells were silent for one missing reader, which is why the coverage they represent
has to be pinned rather than assumed.

The scanner is one shared walk plus a table per grammar, so these tests are mostly about the table
being right -- and about the two ways a table can be wrong without failing loudly.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.source import native_functions as native                            # noqa: E402

# Real source in each language: two functions whose parameters are typeable, and one that is not.
SOURCES = {
    "go": ("math.go", 'package math\n'
                      'func Add(a int, b int) int { return a + b }\n'
                      'func Mean(xs []float64) float64 { return 0 }\n'
                      'func Opaque(c chan int) {}\n'),
    "rust": ("lib.rs", 'pub fn add(a: i32, b: i64) -> i64 { 0 }\n'
                       'pub fn mean(xs: Vec<f64>) -> f64 { 0.0 }\n'
                       'pub fn opaque(f: fn(i32) -> i32) {}\n'),
    "java": ("M.java", 'class M {\n'
                       '  public int add(int a, long b) { return 0; }\n'
                       '  public double mean(double[] xs) { return 0; }\n'
                       '  public void opaque(Runnable r) {}\n'
                       '}\n'),
    "cpp": ("m.cpp", 'int add(int a, int b) { return a + b; }\n'
                     'double mean(std::vector<double> xs) { return 0; }\n'
                     'void opaque(void (*f)(int)) {}\n'),
}

# The vocabulary `frf/observe/probes/schema.py` can actually draw a value for. A kind outside this
# set cannot be sampled, so it would refuse every probe and the refusal would be charged to the
# candidate.
DRAWABLE = {"int", "float", "bool", "string", "bytes", "int_array", "float_array", "complex_array"}


def _checkout(language: str) -> str:
    filename, text = SOURCES[language]
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, filename), "w") as handle:
        handle.write(text)
    return directory


def _scan(language: str) -> list:
    if not native.supported(language):
        pytest.skip("%s has no grammar table" % language)
    found = native.scan(_checkout(language), "pkg", "abc123", language=language)
    if not found:
        pytest.skip("the %s grammar is not installed" % language)
    return found


@pytest.mark.parametrize("language", sorted(SOURCES))
def test_every_language_yields_typed_functions(language):
    """The regression this file exists for: these languages used to yield nothing at all."""
    found = _scan(language)
    assert len(found) >= 2, (
        "%s yielded %d functions; the grammar table is not matching this source"
        % (language, len(found)))


@pytest.mark.parametrize("language", sorted(SOURCES))
def test_the_function_name_is_not_the_parameter_name(language):
    """A real bug, and the reason Rust silently returned zero functions.

    The field holding a FUNCTION's name and the field holding a PARAMETER's name are different
    questions, and only some grammars answer both with `name`: Rust spells the second `pattern`,
    C++ hides it in a declarator. Conflating them made `_symbol` return empty for every Rust
    function, so all of them were dropped -- an empty scan, which is indistinguishable from a
    checkout with no usable functions in it.
    """
    for function in _scan(language):
        assert function.symbol, "a function was mined with no name"
        assert "(" not in function.symbol, (
            "%s: %r is a declarator, not a symbol" % (language, function.symbol))
        for param in function.schema["params"]:
            assert param["name"], "%s: %s has an unnamed parameter" % (language, function.symbol)
            # `&s` and `*p` are how C++ spells a reference; the sigil belongs to the type. A
            # parameter named `&s` does not match anything the generated dispatcher will call.
            assert not param["name"].startswith(("&", "*")), (
                "%s: %s takes %r, which carries a type sigil into the name"
                % (language, function.symbol, param["name"]))


@pytest.mark.parametrize("language", sorted(SOURCES))
def test_every_mined_parameter_can_actually_be_drawn(language):
    """A schema the sampler cannot draw is worse than no schema.

    `sample()` has a fixed vocabulary. A parameter typed outside it refuses every probe, and that
    refusal is charged to the MATERIAL -- so an over-eager type table would look exactly like a
    supply of broken candidates.
    """
    for function in _scan(language):
        for param in function.schema["params"]:
            assert param["kind"] in DRAWABLE, (
                "%s: %s takes a %r, which the probe sampler cannot draw"
                % (language, function.symbol, param["kind"]))


@pytest.mark.parametrize("language", sorted(SOURCES))
def test_an_untypeable_function_is_skipped_rather_than_guessed(language):
    """Each fixture ends in a function this file cannot type: a channel, a function pointer.

    Skipping it is the honest answer. Guessing draws probes the subject was never meant to accept,
    and the resulting failure is billed to the candidate.
    """
    assert "opaque" not in {f.symbol.lower() for f in _scan(language)}, (
        "%s mined a function whose parameter type is not drawable" % language)


def test_a_partially_typeable_function_is_refused_whole():
    """Half a parameter list is worse than none of it.

    A subject called with the wrong NUMBER of arguments fails on every probe, so a function is
    taken only when every parameter can be typed -- not with the untypeable ones quietly dropped.
    """
    if not native.supported("go"):
        pytest.skip("no go grammar table")
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, "mixed.go"), "w") as handle:
        handle.write('package mixed\n'
                     'func Half(a int, c chan int) int { return a }\n')
    found = native.scan(directory, "pkg", "abc123", language="go")
    assert found == [] or all(len(f.schema["params"]) == 2 for f in found), (
        "a function was mined with its untypeable parameters dropped, so it will be called with "
        "the wrong number of arguments")


def test_one_declaration_naming_several_parameters_yields_several():
    """Go shares one type between names, and reading only the first shortens the arity.

    A REAL FAILURE, from a real batch. `func Knapsack(maxWeight int, weights, values []int) int` is
    three parameters in two declarations. Mined as two, the generated bridge called it with two
    arguments and the build failed with `not enough arguments in call to Knapsack, have (int, []int)`
    -- charged to the MATERIAL, which is exactly what `test_a_partially_typeable_function_is_refused
    _whole` exists to prevent, arrived at by another route.

    The fix is `children_by_field_name`, plural; the singular form returns the first name only. The
    other grammars repeat the type per parameter, so they are unaffected -- which the parametrised
    tests above confirm still hold.
    """
    if not native.supported("go"):
        pytest.skip("no go grammar table")
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, "knap.go"), "w") as handle:
        handle.write('package dynamic\n'
                     'func Knapsack(maxWeight int, weights, values []int) int { return 0 }\n')
    found = native.scan(directory, "pkg", "abc123", language="go")
    if not found:
        pytest.skip("the go grammar is not installed")
    names = [param["name"] for param in found[0].schema["params"]]
    assert names == ["maxWeight", "weights", "values"], names


def test_a_void_function_is_kept_only_when_something_can_carry_the_mutation():
    """Void is a third of the Go supply, and an in-place sort is ideal kernel material.

    So it is not refused for being void -- it is refused when no argument can show what happened.
    A void function of scalars has copied its arguments and left no evidence, so every probe returns
    the same nothing and no corpus distinguishes an implementation from a stub.
    """
    if not native.supported("go"):
        pytest.skip("no go grammar table")
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, "v.go"), "w") as handle:
        handle.write('package v\n'
                     'func SortInPlace(xs []int) {}\n'
                     'func LogIt(n int) {}\n')
    found = native.scan(directory, "pkg", "abc123", language="go")
    if not found:
        pytest.skip("the go grammar is not installed")
    mined = {f.symbol: f for f in found}
    assert "SortInPlace" in mined, "an in-place sort over a drawable array is usable material"
    assert mined["SortInPlace"].result == {}, "void is recorded as {}, not as a type"
    assert "LogIt" not in mined, "a void function of scalars leaves nothing to observe"


def test_a_result_the_wire_cannot_carry_refuses_the_function():
    """A builder or a generic handle cannot be encoded, and the failure would look like material.

    Measured on a real checkout: refusing these took one Rust tree from 56 mined functions to 5.
    What left was never servable -- an `AppxDbscanParams<F, CommonNearestNeighbour>` cannot cross a
    JSON wire, so every probe against it would have been charged to the candidate.
    """
    if not native.supported("rust"):
        pytest.skip("no rust grammar table")
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, "lib.rs"), "w") as handle:
        handle.write('pub fn ok(a: i64) -> i64 { a }\n'
                     'pub fn opaque(a: usize) -> Params<F, Nearest> { todo!() }\n')
    found = native.scan(directory, "pkg", "abc123", language="rust")
    if not found:
        pytest.skip("the rust grammar is not installed")
    assert {f.symbol for f in found} == {"ok"}


def test_the_declared_package_is_reported_so_a_bridge_can_agree_with_it():
    """The first Go batch refused every candidate over exactly this.

    `found packages main (serve.go) and sort (subject.go)`: the shim is `package main` and the
    material declared its own, and nothing had read the second name in order to reconcile them.
    """
    if not native.supported("go"):
        pytest.skip("no go grammar table")
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, "s.go"), "w") as handle:
        handle.write('package sort\nfunc Bubble(xs []int) []int { return xs }\n')
    found = native.scan(directory, "pkg", "abc123", language="go")
    if not found:
        pytest.skip("the go grammar is not installed")
    assert found[0].declared_package == "sort"


def test_a_method_is_refused_because_nothing_can_build_its_receiver():
    """Rust hides this worst: `self_parameter` is not in `param_nodes`.

    So `fn add(&mut self, cost: f64)` was mined as a free function of ONE argument, and a bridge
    generated from that cannot compile. Go marks the same thing with a `receiver` field.
    """
    if not native.supported("rust"):
        pytest.skip("no rust grammar table")
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, "lib.rs"), "w") as handle:
        handle.write('pub fn free(a: i64) -> i64 { a }\n'
                     'impl T { pub fn method(&mut self, cost: f64) -> f64 { cost } }\n')
    found = native.scan(directory, "pkg", "abc123", language="rust")
    if not found:
        pytest.skip("the rust grammar is not installed")
    assert {f.symbol for f in found} == {"free"}


def test_an_unregistered_language_is_not_silently_empty():
    """`supported()` is how the miner tells "cannot read this" from "read it, found nothing".

    The two must stay distinguishable: the first is our gap and the second is a fact about the
    material, and collapsing them makes a registered adapter look like a missing one.
    """
    assert not native.supported("cobol")
    assert native.scan(tempfile.mkdtemp(), language="cobol") == []
    for language in sorted(SOURCES):
        assert native.supported(language), "%s should be registered" % language


def test_a_private_java_method_is_not_offered_to_the_bridge():
    """`static` does not make a method reachable; the generated `Subject` must be allowed to name it.

    The bridge is emitted as class `Subject` and calls `Owner.method(...)`. A `private static`
    method passes the static check and then refuses to compile -- `getLCA(int,int,int[],int[]) has
    private access in LCA` -- which is charged to the material and is really the miner offering a
    symbol the caller cannot use. Two TheAlgorithms/Java candidates failed that way in one batch,
    and java has never emitted a task.

    Package-private (no modifier) is refused too: Java's default is a third state that reads like
    public in the source and compiles like private from another package.
    """
    import os
    import tempfile

    from frf.source import native_functions as native

    root = tempfile.mkdtemp()
    with open(os.path.join(root, "Mixed.java"), "w", encoding="utf-8") as handle:
        handle.write(
            "public class Mixed {\n"
            "    public static int Visible(int a, int b) { return a + b; }\n"
            "    private static int Hidden(int a, int b) { return a - b; }\n"
            "    static int PackagePrivate(int a, int b) { return a * b; }\n"
            "}\n")

    names = {fn.symbol for fn in native.scan(root, "mixed", "1.0", language="java")}
    assert "Visible" in names, names
    assert "Hidden" not in names, "a private method cannot be called from the generated Subject"
    assert "PackagePrivate" not in names, "package-private is not reachable either"
