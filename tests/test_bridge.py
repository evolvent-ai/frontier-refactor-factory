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
    """A language with no bridge is a stated gap, not source generated in the wrong language.

    Handing a Ruby subject a Go bridge would fail the build as though the material were broken, which
    is the confusion this whole layer exists to prevent. `dispatch.py` refuses the same way.

    Ruby is here because it needs NO bridge -- serve.rb resolves the symbol itself -- so asking for one
    is a caller mistake rather than a missing feature.
    """
    for language in ("ruby", "python", "javascript", "cobol"):
        with pytest.raises(bridge.Unsupported):
            bridge.source(language, symbol="f", params=[_ints()], result=_ints())


def test_every_static_language_generates_a_bridge_declaring_its_own_entry_point():
    """Four shims, four different contracts, and each bridge has to satisfy its own.

    Verified against real toolchains elsewhere (rustc 1.83, javac 21, g++ 13.3, go); this pins the
    declaration each one must contain, which is what breaks first if a template is edited.
    """
    expected = {
        "go": "func Entry(args []interface{}) (interface{}, error)",
        "rust": "pub fn entry(args: &crate::Json) -> Result<crate::Json, String>",
        "java": "public static Object entry(java.util.List<Object> args)",
        "cpp": 'extern "C" const char *entry(const char *args_json)',
    }
    for language, declaration in expected.items():
        source = bridge.source(language, symbol="f", params=[_ints()],
                               result={"kind": "int", "native": "int"},
                               owner="M" if language == "java" else "")
        assert declaration in source, language


def test_a_java_bridge_without_its_owning_class_is_refused():
    """Every Java method lives in a class, so the bridge calls `Owner.method(...)`.

    Without the name there is nothing to call and no way to construct an instance, so this refuses
    rather than emitting a bridge that cannot compile.
    """
    with pytest.raises(bridge.Unsupported):
        bridge.source("java", symbol="f", params=[_ints()],
                      result={"kind": "int", "native": "int"})


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
    """One source of truth, so a new generator is automatically a newly supported language.

    The four static languages have one each. The dynamic ones deliberately do NOT: their shims resolve
    a symbol by name, so a bridge would be a second mechanism for something already handled -- and
    `observe/call.servable` is what asks the combined question.
    """
    for language in ("go", "rust", "java", "cpp"):
        assert bridge.supported(language), language
    for language in ("python", "javascript", "typescript", "ruby", "", "cobol"):
        assert not bridge.supported(language), language


# --------------------------------------------------------------------- what an emitted task ships


class _Corpus:
    """The three fields `write_tests` reads. A real Corpus needs a freeze to build."""

    expectations = ()
    inputs: dict = {}
    timed = ()


class _Material:
    """A mined Go function, as `_locate` would have produced it."""

    def __init__(self, source_path):
        self.identity = "github:o/r@abc123#s/bubble.go.Bubble"
        self.language = "go"
        self.source_path = source_path
        self.symbol = "Bubble"
        self.description = "sort"
        self.forbidden = ()
        self.binding = {"params": [_ints()], "result": _ints(), "package": "sort"}


def _emit(tmp_path):
    from types import SimpleNamespace
    from frf.observe.call import package as call_package

    subject = tmp_path / "bubble.go"
    subject.write_text("package sort\n"
                       "func main() { println(1) }\n"
                       "func Bubble(array []int) []int { return array }\n")
    out = tmp_path / "task"
    (out / "environment").mkdir(parents=True)
    call_package.write_tests(str(out), _Corpus(),
                             spec=SimpleNamespace(language="go", name="t", scale="kernel"),
                             material=_Material(str(subject)))
    return out


def test_an_emitted_static_task_ships_the_bridge_it_needs_to_build(tmp_path):
    """E7 replay failed for exactly this, having passed freeze, adequacy and evidence.

    The emitted package laid the subject out with its OWN copy of "copy the source, write the shim",
    so it was missing everything `materialise` had learned: no bridge.go, and a subject.go still
    saying `package sort` beside a serve.go saying `package main`. The reference could not build out
    of the package it shipped in, reported as `the submission exited without answering`.

    This is the class of fault E7 exists to catch -- a reference built somewhere the package does not
    contain -- so it is pinned on both sides of the wall.
    """
    out = _emit(tmp_path)
    for side in (out / "tests" / "reference", out / "environment"):
        assert (side / "bridge.go").is_file(), "%s ships no bridge; serve.go has no Entry" % side
        assert "func Entry(" in (side / "bridge.go").read_text()
        subject = (side / "subject.go").read_text()
        assert subject.startswith("package main"), "the package clause was not reconciled"
        assert "frfUnusedMain(" in subject, "a second main would collide with the shim's"


def test_run_sh_compiles_the_bridge_and_keeps_paths_portable(tmp_path):
    """run.sh ships inside the package and is started after a cd, so paths must stay relative.

    `bridged` is passed to `commands()` rather than probed with os.path.exists for this reason: the
    workdir resolves to ".", so a probe would have asked about the factory's own directory and
    dropped bridge.go from a build that needs it.
    """
    run = (_emit(tmp_path) / "environment" / "run.sh").read_text()
    assert "bridge.go" in run, "the bridge is written but never compiled"
    assert "/tmp" not in run and str(tmp_path) not in run, "run.sh carries this machine's paths"


def test_both_sides_of_the_wall_are_laid_out_identically(tmp_path):
    """The timing comparison is only meaningful if the two sides are byte-identical.

    A reference served differently from the candidate measures the difference between two harnesses.
    """
    out = _emit(tmp_path)
    reference, environment = out / "tests" / "reference", out / "environment"
    for name in ("subject.go", "bridge.go", "serve.go", "run.sh"):
        assert (reference / name).read_text() == (environment / name).read_text(), name
