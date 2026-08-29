"""Reading Ruby functions, where the language writes no types at all.

WHY RUBY GETS ITS OWN READER. `native_functions` takes a type off the grammar, because Go, Rust, Java
and C++ write one at every parameter. `javascript_functions` reads a TypeScript annotation or a JSDoc
`@param {number}`. Ruby has neither: the only honest source of a type is a comment the author chose to
write, and the only source of STRUCTURE is a real parse.

The structure matters as much as the types here, which is the part that is easy to miss. `serve.rb`
does `require_relative 'subject'` and then `send(ENTRY, *args)` against the main object, so a method
inside `class Foo` is unreachable however well documented it is. In the checkouts on hand that is 1034
top-level definitions against 850 indented ones -- so getting it wrong either halves the supply or
emits candidates that spend a full freeze to fail with NoMethodError.

WHAT THIS SUPPLY REALLY LOOKS LIKE. Across 202 Ruby files on hand, 1884 definitions carried 4 type
annotations between them. That is the honest reason a Ruby call-scale cell produces little: not a
missing reader, but material that does not say what its parameters are. Refusing to guess is the same
rule the JavaScript reader follows, and for the same reason -- a guessed schema draws probes the
subject never accepted, and the refusal is then charged to the candidate.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from frf.source import ruby_functions as ruby


def _scan(text: str, name: str = "m.rb") -> list:
    if not ruby.supported():
        pytest.skip("the ruby grammar is not installed")
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        handle.write(text)
    return ruby.scan(directory, "pkg", "abc123")


def test_a_documented_top_level_function_is_mined():
    """The braced form real Ruby algorithm repositories use, and the one found on disk."""
    found = _scan("# @param {Integer[]} nums\n"
                  "# @param {Integer} target\n"
                  "def two_sum(nums, target)\n  []\nend\n")
    assert [f.symbol for f in found] == ["two_sum"]
    assert [p["kind"] for p in found[0].schema["params"]] == ["int_array", "int"]
    assert [p["name"] for p in found[0].schema["params"]] == ["nums", "target"]


def test_the_yard_spelling_is_read_too():
    """Two spellings exist in the wild: `@param [Integer] x` and `@param {Integer} x`.

    Two patterns rather than one permissive one -- a pattern loose enough to read either would also
    read prose, and a wrong type is worse here than no type.
    """
    found = _scan("# @param [Array<Integer>] xs\ndef total(xs)\n  0\nend\n")
    assert [f.symbol for f in found] == ["total"]
    assert found[0].schema["params"][0]["kind"] == "int_array"


def test_an_undocumented_function_is_refused_rather_than_guessed():
    """Most of Ruby, and refusing it is the point.

    A guessed schema draws probes the subject was never meant to accept; the failure is charged to
    the material and is indistinguishable from a broken candidate.
    """
    assert _scan("def mystery(a, b)\n  a\nend\n") == []


def test_a_method_inside_a_class_is_not_mined():
    """`send` on the main object cannot reach it, however well typed it is.

    Mining one would pay for a whole freeze and then fail with NoMethodError -- and that failure would
    be recorded against the material rather than against this reader.
    """
    found = _scan("class Holder\n"
                  "  # @param {Integer} a\n"
                  "  def scaled(a)\n    a * 2\n  end\n"
                  "end\n")
    assert found == []


def test_a_parameter_list_the_wire_cannot_express_is_refused():
    """The wire sends a positional JSON array, so a splat or a keyword cannot be called correctly."""
    assert _scan("# @param {Integer} a\ndef splatty(a, *rest)\n  a\nend\n") == []
    assert _scan("# @param {Integer} a\ndef keyworded(a, scale: 1)\n  a\nend\n") == []


def test_a_partially_documented_function_is_refused_whole():
    """Half a parameter list is worse than none of it.

    A subject called with the wrong NUMBER of arguments fails every probe, so the untyped parameters
    are not quietly dropped -- the whole function goes.
    """
    assert _scan("# @param {Integer} a\ndef half(a, b)\n  a\nend\n") == []


def test_a_docblock_belonging_to_an_earlier_function_is_not_reused():
    """Comments are read UPWARDS and stopped by the first non-comment line.

    Otherwise a function is typed from its neighbour's parameters, names and all, and the result looks
    exactly like a correct schema.
    """
    found = _scan("# @param {Integer[]} xs\n"
                  "def documented(xs)\n  xs\nend\n"
                  "\n"
                  "def undocumented(xs)\n  xs\nend\n")
    assert [f.symbol for f in found] == ["documented"]


def test_a_file_that_prints_at_load_time_is_skipped():
    """Output written at import corrupts the JSON-lines wire before a single probe is answered.

    Column zero only: `puts` inside a function is observable behaviour and stays the subject's own.
    """
    assert _scan("puts 'banner'\n# @param {Integer} a\ndef f(a)\n  a\nend\n") == []
    kept = _scan("# @param {Integer} a\ndef f(a)\n  puts a\n  a\nend\n")
    assert [f.symbol for f in kept] == ["f"]


def test_a_type_this_wire_cannot_carry_is_refused():
    """The vocabulary is what `probes/schema.py` can draw a value for, and nothing beyond it."""
    assert _scan("# @param {Hash} h\ndef takes_hash(h)\n  h\nend\n") == []
    assert _scan("# @param {Object} o\ndef takes_object(o)\n  o\nend\n") == []


def test_the_miner_routes_ruby_to_this_reader():
    """`function_miner.supported` is what the capability registry is held against.

    Ruby reached the repo scale only for as long as this reader did not exist; the registry now
    declares kernel and module, and `tests/test_capabilities.py` checks that against the mechanism.
    """
    from frf.source.function_miner import supported

    if not ruby.supported():
        pytest.skip("the ruby grammar is not installed")
    assert supported("ruby")
