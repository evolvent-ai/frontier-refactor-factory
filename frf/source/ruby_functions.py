"""Finding callable Ruby functions, with types taken from what the author wrote down.

WHY RUBY IS NEITHER OF THE OTHER TWO READERS. `native_functions` reads a type off the grammar,
because Go, Rust, Java and C++ write one at every parameter. `javascript_functions` reads TypeScript
annotations or a JSDoc `@param {number}`. Ruby has no parameter types in the language at all, so the
only honest source is a comment the author chose to write -- which makes this closer to the JavaScript
reader in KIND, and closer to the native one in MECHANISM, since the structure has to come from a real
parse rather than a regex.

WHY THE STRUCTURE NEEDS A PARSER AND NOT A PATTERN. `serve.rb` does `require_relative 'subject'` and
then `send(ENTRY, *args)` against the main object, so only a TOP-LEVEL `def` is reachable: a method
inside `class Foo` is a method on Foo and no amount of splatting finds it. Telling those apart is a
question about nesting, which is what a grammar answers and a regex guesses at. In the checkouts on
hand it is 1034 top-level definitions against 850 indented ones, so guessing wrong would either lose
half the supply or emit candidates that cannot be served.

WHAT IS DELIBERATELY REFUSED, and it is most of Ruby. A function whose parameters carry no written
type is skipped, exactly as the JavaScript reader skips untyped JavaScript: a guessed schema draws
probes the subject never accepted, and the resulting refusal is charged to the candidate, so a bad
guess here is indistinguishable from bad material. This is a REAL limit on supply rather than a
formality -- across 202 Ruby files on hand, 1884 definitions carried 4 type annotations between them.
A cell that produces nothing for this reason has produced an evidenced refusal, not a silent zero.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

# What a Ruby type name is worth as a probe schema, keyed lowercase. The vocabulary is the one
# `frf/observe/probes/schema.py` can actually draw a value for; anything absent makes its function
# unminable, which is the honest outcome.
_INT = {"kind": "int", "low": -1000, "high": 1000}
_FLOAT = {"kind": "float"}
_BOOL = {"kind": "bool"}
_STRING = {"kind": "string", "size": "n"}
_INTS = {"kind": "int_array", "size": "n", "dtype": "int64"}
_FLOATS = {"kind": "float_array", "size": "n", "dtype": "float64"}

_TYPES = {
    "integer": _INT, "int": _INT, "fixnum": _INT, "numeric": _FLOAT,
    "float": _FLOAT, "double": _FLOAT,
    "string": _STRING, "str": _STRING, "symbol": _STRING,
    "boolean": _BOOL, "bool": _BOOL, "trueclass": _BOOL, "falseclass": _BOOL,
}

# BOTH SPELLINGS, because the material uses both. YARD is `@param [Integer] name`, with the type in
# brackets before the name; the JSDoc-ish form that Ruby algorithm repositories favour is
# `@param {Integer[]} name`. Two patterns rather than one permissive one: a pattern loose enough to
# read either would also read prose, and a wrong type is worse here than no type.
_YARD = re.compile(r"@param\s+\[([^\]]+)\]\s+([A-Za-z_][\w]*)")
_BRACED = re.compile(r"@param\s+\{([^}]+)\}\s+([A-Za-z_][\w]*)")


def _type_schema(spelling: str) -> dict | None:
    """One written type -> what a probe may draw for it, or None if this wire cannot carry it."""
    value = spelling.strip().lower()
    # `Array<Integer>`, and the nilable spellings YARD uses for an optional parameter.
    value = value.replace("nil", "").strip(", ")
    if value.startswith("array<") and value.endswith(">"):
        return _type_schema(value[6:-1] + "[]")
    if value.endswith("[]"):
        base = value[:-2].strip()
        if base in ("integer", "int", "fixnum"):
            return dict(_INTS)
        if base in ("float", "double", "numeric"):
            return dict(_FLOATS)
        return None
    found = _TYPES.get(value)
    return dict(found) if found else None


def _documented_types(source: str, line: int) -> dict:
    """The `@param` types written immediately above the definition on `line`.

    Read UPWARDS from the definition and stopped by the first line that is not a comment, so a
    docblock belonging to some earlier function cannot be attributed to this one. That
    misattribution would type a function from its neighbour's parameters -- names and all -- and the
    result would look exactly like a correct schema.
    """
    types: dict = {}
    lines = source.splitlines()
    index = line - 1
    while index >= 0:
        text = lines[index].strip()
        if not text.startswith("#"):
            break
        for pattern in (_YARD, _BRACED):
            for spelling, name in pattern.findall(text):
                types.setdefault(name, spelling)
        index -= 1
    return types


def _parser():
    """A Ruby parser, or None when the grammar is not installed.

    None rather than an exception, for the reason `native_functions._parser` gives: a missing grammar
    is our own deployment gap, and the caller turns it into a stated refusal instead of an empty scan
    that reads as unsuitable material.
    """
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_ruby
        return Parser(Language(tree_sitter_ruby.language()))
    except Exception:                                   # noqa: BLE001 -- absent or ABI-mismatched
        return None


def _is_top_level(node) -> bool:
    """Whether `send` on the main object can reach this definition.

    `serve.rb` requires the subject and sends to the main object, so a method defined inside a class
    or module is not reachable however well typed it is -- and mining one would spend a whole freeze
    to fail with NoMethodError.
    """
    parent = node.parent
    while parent is not None:
        if parent.type in ("class", "module", "singleton_class"):
            return False
        parent = parent.parent
    return True


def _parameter_names(node, source: bytes) -> list | None:
    """The parameter names, or None if the list has a shape this wire cannot express.

    A splat, a block or a keyword argument is refused rather than approximated: the wire sends a
    positional JSON array, so `def f(*rest)` and `def f(a, b: 1)` would be called with arguments they
    do not accept, and the refusal would be charged to the candidate.
    """
    if node is None:
        return []
    names = []
    for child in node.children:
        if child.type in ("(", ")", ","):
            continue
        if child.type != "identifier":
            # splat_parameter, block_parameter, keyword_parameter, optional_parameter, ...
            return None
        names.append(source[child.start_byte:child.end_byte].decode("utf-8", "replace"))
    return names


def supported() -> bool:
    """Whether this reader can run at all -- that is, whether the grammar is installed."""
    return _parser() is not None


def scan(root: str, package: str = "", version: str = "") -> list:
    """Every minable Ruby function under `root`, in the shape the other readers return.

    Deliberately the same contract as `functions.scan`, `javascript_functions.scan` and
    `native_functions.scan`: the miner should not care which reader found a function.
    """
    parser = _parser()
    if parser is None:
        return []

    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "spec", "test", "tests", "vendor", "features"}]
        for filename in filenames:
            if not filename.endswith(".rb") or filename.endswith("_spec.rb"):
                continue
            path = os.path.join(dirpath, filename)
            try:
                raw = open(path, "rb").read()
            except OSError:
                continue
            if len(raw) > 400_000:
                continue
            text = raw.decode("utf-8", "replace")
            # Output written at load time corrupts the JSON-lines wire before the shim can answer a
            # single probe. Only column zero: `puts` inside a function is observable behaviour and
            # remains the subject's own business.
            if re.search(r"(?m)^(?:puts|print|p)\s", text):
                continue
            try:
                tree = parser.parse(raw)
            except Exception:                           # noqa: BLE001 -- a grammar can reject
                continue
            module = os.path.relpath(path, root)
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                stack.extend(node.children)
                if node.type != "method" or not _is_top_level(node):
                    continue
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                symbol = text[name_node.start_byte:name_node.end_byte]
                # `?` and `!` are legal in a Ruby method name but not in most task identities, and a
                # predicate returning a boolean is thin material anyway.
                if not symbol or symbol.startswith("_") or not symbol.isidentifier():
                    continue
                names = _parameter_names(node.child_by_field_name("parameters"), raw)
                # No parameters means nothing to sample; an unexpressible list is refused above.
                if not names:
                    continue
                documented = _documented_types(text, node.start_point[0])
                schema = []
                for parameter in names:
                    described = _type_schema(documented.get(parameter, ""))
                    if described is None:
                        # UNTYPED IS REFUSED WHOLE, exactly as an untypeable parameter is in the
                        # static reader: half a parameter list calls the subject with the wrong
                        # arity, and every probe then fails against the material's account.
                        schema = None
                        break
                    described["name"] = parameter
                    schema.append(described)
                if not schema:
                    continue
                found.append(SimpleNamespace(
                    package=package, version=version, module=module, symbol=symbol,
                    path=path, schema={"params": schema}, doc=""))
    found.sort(key=lambda item: (item.module, item.symbol))
    return found
