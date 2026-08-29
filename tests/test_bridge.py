"""The generated call bridge: the third part of the call seam.

WHAT THIS FILE PINS. A dynamic shim binds a mined symbol at run time; a static one cannot, and the
gap between "the miner found a function" and "the shim can serve it" is generated code. The failures
below are all real ones, taken from the first Go kernel batch, which refused every candidate:

    found packages main (serve.go) and sort (subject.go)
    ./serve.go:137:17: undefined: Entry

Neither message is about the material, and both were produced with a miner and a shim both present.

WHAT NEEDS A COMPILER AND WHAT DOES NOT. Whether the generated Go actually builds and answers over the
wire is checked by `test_wire_roundtrip.py` on a host that has Go. Everything here is structural: which
declarations are emitted, which collisions are reconciled, and which material is refused rather than
turned into source that cannot compile. Those are the parts that go wrong silently.
"""
from __future__ import annotations

import pytest

from frf.observe.call import bridge


def _ints(name="array", native="[]int"):
    return {"kind": "int_array", "dtype": "int64", "size": "n", "name": name, "native": native}


def test_a_bridge_declares_the_entry_point_the_shim_calls():
    """`undefined: Entry` was the second failure, once the package name was fixed by hand."""
    source = bridge.source("go", symbol="BubbleSort", params=[_ints()],
                           result=_ints(native="[]int"), package="sort")
    assert "package main" in source
    assert "func Entry(args []interface{}) (interface{}, error)" in source
    assert "BubbleSort(arg0)" in source


def test_arity_is_checked_before_the_subject_is_called():
    """A wrong-length argument list is the bridge's refusal, not a panic inside the subject.

    Without this the slice index panics, `serve.go` recovers it, and the reply blames the subject for
    a fault in the request -- which is then frozen into an expectation as though it were behaviour.
    """
    source = bridge.source("go", symbol="Add",
                           params=[{"kind": "int", "name": "a", "native": "int"},
                                   {"kind": "int", "name": "b", "native": "int"}],
                           result={"kind": "int", "native": "int"})
    assert "len(args) != 2" in source


def test_an_integral_argument_is_refused_rather_than_truncated():
    """JSON has one number type, so every int arrives as a float64.

    Truncating 2.5 to 2 would have the subject answer a question it was never asked, and the answer
    would be frozen as correct.
    """
    source = bridge.source("go", symbol="F", params=[{"kind": "int", "name": "n", "native": "int"}],
                           result={"kind": "int", "native": "int"})
    assert "number != float64(int(number))" in source


def test_a_spelling_that_differs_from_the_kind_is_converted():
    """`[]int32` and `[]int` are both `int_array`, and a converter yields `[]int`.

    Handing `[]int` to a function declared `[]int32` does not compile. This is why the miner keeps the
    source's own spelling beside the kind: the kind alone cannot generate a call.
    """
    source = bridge.source("go", symbol="F", params=[_ints(native="[]int32")],
                           result={"kind": "int", "native": "int"})
    assert "make([]int32, len(arg0))" in source
    assert "int32(item)" in source


def test_a_void_function_answers_with_what_it_mutated():
    """A third of the mined Go functions return nothing, because in-place sorting is ordinary.

    Refusing them would discard the material the kernel scale most wants. The observable behaviour is
    the mutation, so the array argument is returned after the call.
    """
    source = bridge.source("go", symbol="SortInPlace", params=[_ints()], result={})
    assert "SortInPlace(arg0)" in source
    assert "return arg0, nil" in source


def test_a_void_function_with_nothing_to_mutate_is_refused():
    """A void function of scalars leaves no evidence anywhere.

    Every probe would return the same nothing, so no corpus could tell an implementation from a stub.
    The miner already refuses these; the generator refuses them too rather than emitting a bridge
    that returns a constant.
    """
    with pytest.raises(bridge.Unsupported):
        bridge.source("go", symbol="Log",
                      params=[{"kind": "int", "name": "n", "native": "int"}], result={})


def test_an_unconvertible_argument_is_refused_not_guessed():
    """A kind with no conversion would emit source that does not compile.

    The failure would arrive as a build error and be charged to the material, which is the confusion
    this whole layer exists to prevent.
    """
    with pytest.raises(bridge.Unsupported):
        bridge.source("go", symbol="F",
                      params=[{"kind": "map", "name": "m", "native": "map[string]int"}],
                      result={"kind": "int", "native": "int"})


def test_a_language_without_a_generator_refuses_loudly():
    """Rust, Java and C++ have static shims and no bridge yet.

    Generating Go source for a Rust subject would fail the build as though the material were broken,
    so the gap is stated instead. `dispatch.py` refuses the same way for the same reason.
    """
    for language in ("rust", "java", "cpp", "ruby"):
        with pytest.raises(bridge.Unsupported):
            bridge.source(language, symbol="f", params=[_ints()], result=_ints())


def test_the_package_clause_is_reconciled_to_the_shim():
    """The first failure of the first batch, and it is not the material's fault.

    Go requires one package per directory; the shim is `package main` and real material declares
    whatever the repository chose.
    """
    out = bridge.reconcile("go", "package sort\n\nfunc BubbleSort(a []int) []int { return a }\n")
    assert out.startswith("package main\n")
    assert "func BubbleSort" in out


def test_a_second_main_is_renamed_rather_than_deleted():
    """A repository that ships a program has `func main`, and so does the shim.

    Renamed, not cut: deleting spans is how a file gets truncated mid-expression, an unused function
    is legal in Go, and nothing calls a `main`, so no behaviour of the subject changes.
    """
    out = bridge.reconcile("go", "package cli\nfunc main() { println(1) }\nfunc F(a []int) int { return 0 }\n")
    assert "func main(" not in out
    assert "frfUnusedMain(" in out
    assert "func F(a []int) int" in out


def test_reconciling_twice_is_reconciling_once():
    """The mutant path materialises a file that is already at its destination.

    `materialise` reads then writes the same path, so a non-idempotent reconcile would corrupt the
    subject on the second pass -- and E3 would read the corruption as a detected mutation.
    """
    once = bridge.reconcile("go", "package sort\nfunc main() {}\nfunc F(a []int) int { return 0 }\n")
    assert bridge.reconcile("go", once) == once


def test_a_language_needing_no_reconciliation_is_returned_unchanged():
    """Rust reaches its subject as `mod subject`, so its file needs no surgery.

    Returned unchanged so a caller can apply this unconditionally instead of deciding per language,
    which is the kind of decision that ends up in two places and then disagrees.
    """
    text = "pub fn entry(a: i64) -> i64 { a }\n"
    assert bridge.reconcile("rust", text) == text
    assert bridge.reconcile("python", text) == text


def test_supported_names_only_languages_with_a_generator():
    """One source of truth, so a new generator is automatically a newly supported language."""
    assert bridge.supported("go")
    assert not bridge.supported("rust")
    assert not bridge.supported("")
