"""Finding callable functions in a statically typed language, with their argument types.

WHY THIS FILE EXISTS. Before it, `function_miner` had exactly two scanners -- Python's `ast` and a
regex reader for JavaScript -- and every other language fell to `else: call-adapter-not-registered`
and returned an empty list. So the three call scales could not produce a single task in Go, Rust,
Java or C++: not because the material was unsuitable, but because nothing ever looked at it. That is
15 of the 32 scale x language cells silent for one missing reader.

WHY THE TYPES MATTER AS MUCH AS THE NAMES. A probe corpus here is SAMPLED from a schema, so a
parameter is only usable if its type is known -- `sample()` cannot draw a value for an argument it
cannot describe. The dynamic languages get away with a name alone because their dispatcher looks a
symbol up at runtime; Go, Rust, Java and C++ need the concrete type to generate a static dispatcher
at all. A scanner that returned names without types would be no more useful than no scanner.

ONE TABLE PER LANGUAGE, NOT ONE SCANNER PER LANGUAGE. Grammars disagree about what to call things --
Go says `parameter_declaration`, Rust `parameter`, Java `formal_parameter`, C++ nests it inside a
`function_declarator` -- so the differences live in `_GRAMMARS` as node names and a type map, and the
walk itself is shared. Adding a sixth language is a table entry, not another file.

WHAT IS DELIBERATELY REFUSED. A function whose parameters this file cannot type is skipped rather
than guessed at. A guessed schema draws probes the subject was never meant to accept, and the
resulting refusal is charged to the candidate -- so a bad guess here would look exactly like bad
material, which is the failure mode this factory is built to avoid.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

# What a native type is worth as a probe schema. Keyed by the source spelling with whitespace and
# `const`/`&` removed, so a table entry covers the several ways one type is written.
#
# The values are the vocabulary `frf/observe/probes/schema.py` actually supports; anything absent
# from this map makes its function unminable, which is the honest outcome -- see the module docstring.
_INT = {"kind": "int", "low": -1000, "high": 1000}
_FLOAT = {"kind": "float"}
_BOOL = {"kind": "bool"}
_STRING = {"kind": "string", "size": "n"}
_BYTES = {"kind": "bytes", "size": "n"}
_INTS = {"kind": "int_array", "size": "n", "dtype": "int64"}
_FLOATS = {"kind": "float_array", "size": "n", "dtype": "float64"}

_GO_TYPES = {
    "int": _INT, "int8": _INT, "int16": _INT, "int32": _INT, "int64": _INT,
    "uint": _INT, "uint8": _INT, "uint16": _INT, "uint32": _INT, "uint64": _INT, "rune": _INT,
    "float32": _FLOAT, "float64": _FLOAT,
    "string": _STRING, "bool": _BOOL,
    "[]byte": _BYTES,
    "[]int": _INTS, "[]int32": _INTS, "[]int64": _INTS, "[]uint": _INTS,
    "[]float32": _FLOATS, "[]float64": _FLOATS,
}

_RUST_TYPES = {
    "i8": _INT, "i16": _INT, "i32": _INT, "i64": _INT, "isize": _INT,
    "u8": _INT, "u16": _INT, "u32": _INT, "u64": _INT, "usize": _INT,
    "f32": _FLOAT, "f64": _FLOAT,
    "String": _STRING, "str": _STRING, "bool": _BOOL,
    "Vec<u8>": _BYTES, "[u8]": _BYTES,
    "Vec<i32>": _INTS, "Vec<i64>": _INTS, "Vec<usize>": _INTS,
    "[i32]": _INTS, "[i64]": _INTS, "[usize]": _INTS,
    "Vec<f32>": _FLOATS, "Vec<f64>": _FLOATS, "[f32]": _FLOATS, "[f64]": _FLOATS,
}

_JAVA_TYPES = {
    "int": _INT, "long": _INT, "short": _INT, "byte": _INT, "char": _INT,
    "Integer": _INT, "Long": _INT,
    "float": _FLOAT, "double": _FLOAT, "Double": _FLOAT, "Float": _FLOAT,
    "String": _STRING, "boolean": _BOOL, "Boolean": _BOOL,
    "byte[]": _BYTES,
    "int[]": _INTS, "long[]": _INTS, "Integer[]": _INTS,
    "double[]": _FLOATS, "float[]": _FLOATS,
}

_CPP_TYPES = {
    "int": _INT, "long": _INT, "short": _INT, "size_t": _INT, "unsigned": _INT,
    "unsignedint": _INT, "longlong": _INT, "int64_t": _INT, "int32_t": _INT, "uint32_t": _INT,
    "float": _FLOAT, "double": _FLOAT,
    "std::string": _STRING, "string": _STRING, "char*": _STRING, "bool": _BOOL,
    "std::vector<int>": _INTS, "vector<int>": _INTS,
    "std::vector<long>": _INTS, "std::vector<int64_t>": _INTS,
    "std::vector<double>": _FLOATS, "vector<double>": _FLOATS,
    "std::vector<float>": _FLOATS,
}

# Per grammar: which node is a function, which node is one parameter, which field holds the
# FUNCTION's name (`name_field`), which holds a PARAMETER's name (`param_name_field` -- Rust
# spells it `pattern`, C++ hides it in a declarator), which holds a parameter's type, and how
# that language's types spell themselves.
#
# `params_field` is the function node's field that holds the parameter list; `param_nodes` are the
# node types inside it that count as one parameter (C++ mixes several).
_GRAMMARS = {
    "go": {
        "module": "tree_sitter_go",
        "suffixes": (".go",),
        "functions": ("function_declaration", "method_declaration"),
        "params_field": "parameters",
        "param_nodes": ("parameter_declaration",),
        "name_field": "name",
        "param_name_field": "name",
        "type_field": "type",
        "types": _GO_TYPES,
        "skip_dirs": {"vendor", "testdata", ".git"},
        "skip_files": ("_test.go",),
    },
    "rust": {
        "module": "tree_sitter_rust",
        "suffixes": (".rs",),
        "functions": ("function_item",),
        "params_field": "parameters",
        "param_nodes": ("parameter",),
        "name_field": "name",
        "param_name_field": "pattern",
        "type_field": "type",
        "types": _RUST_TYPES,
        "skip_dirs": {"target", "tests", ".git"},
        "skip_files": (),
    },
    "java": {
        "module": "tree_sitter_java",
        "suffixes": (".java",),
        "functions": ("method_declaration",),
        "params_field": "parameters",
        "param_nodes": ("formal_parameter",),
        "name_field": "name",
        "param_name_field": "name",
        "type_field": "type",
        "types": _JAVA_TYPES,
        "skip_dirs": {"target", "build", "test", ".git"},
        "skip_files": ("Test.java",),
    },
    "cpp": {
        "module": "tree_sitter_cpp",
        "suffixes": (".cpp", ".cc", ".cxx", ".hpp", ".h"),
        "functions": ("function_definition",),
        "params_field": "declarator",           # C++ hides the list inside the declarator
        "param_nodes": ("parameter_declaration",),
        "name_field": "declarator",
        "param_name_field": "declarator",
        "type_field": "type",
        "types": _CPP_TYPES,
        "skip_dirs": {"build", "test", "tests", ".git", "third_party"},
        "skip_files": (),
    },
}


def supported(language: str) -> bool:
    """Whether this file can read `language` at all."""
    return language in _GRAMMARS


def _parser(spec: dict):
    """A parser for one grammar, or None when the grammar is not installed.

    None rather than an exception: a missing grammar is our own deployment gap, and the caller
    turns it into a stated refusal instead of an empty scan that looks like unsuitable material.
    """
    try:
        from tree_sitter import Language, Parser
        grammar = __import__(spec["module"])
        return Parser(Language(grammar.language()))
    except Exception:                                       # noqa: BLE001 -- absent or ABI-mismatched
        return None


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _normalise(spelling: str) -> str:
    """One spelling for a type written several ways.

    `const std::string &s`, `std::string& s` and `std::string s` are the same parameter as far as a
    probe is concerned, so the qualifiers that do not change the VALUE are removed.
    """
    out = spelling.replace("const", " ").replace("mut ", " ")
    for noise in ("&", "*restrict", "\n", "\t"):
        out = out.replace(noise, "*" if noise == "*restrict" else " ")
    out = "".join(out.split())
    # A pointer to char is a string; any other pointer is not something we can draw a value for.
    if out in ("char*", "char**"):
        return "char*"
    return out


def _param_schema(spelling: str, types: dict) -> dict | None:
    normalised = _normalise(spelling)
    if normalised in types:
        return dict(types[normalised])
    # Go and Rust slices carry their element type: `[]float64`, `Vec<i64>`, `&[u8]`.
    stripped = normalised.lstrip("&")
    return dict(types[stripped]) if stripped in types else None


def _find(node, wanted: tuple) -> list:
    """Every descendant whose type is in `wanted`, outermost first."""
    out = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in wanted:
            out.append(current)
        stack.extend(reversed(current.children))
    return out


def _parameters(function, spec: dict, source: bytes) -> list | None:
    """The function's parameters as probe schema, or None if any one of them cannot be typed.

    None for the WHOLE function rather than a shortened list: a subject called with the wrong number
    of arguments fails on every probe, and that failure would be charged to the material.
    """
    holder = function.child_by_field_name(spec["params_field"])
    if holder is None:
        return None
    params = _find(holder, spec["param_nodes"])
    schema = []
    for param in params:
        type_node = param.child_by_field_name(spec["type_field"])
        if type_node is None:
            return None
        described = _param_schema(_text(source, type_node), spec["types"])
        if described is None:
            return None
        name_node = param.child_by_field_name(spec["param_name_field"])
        # A C++ declarator is `&s` or `*p`; the sigil belongs to the type, not the name.
        described["name"] = (_text(source, name_node).lstrip("&*").strip()
                             if name_node is not None else "") or "arg%d" % len(schema)
        schema.append(described)
    return schema


def _symbol(function, spec: dict, source: bytes) -> str:
    name_node = function.child_by_field_name(spec["name_field"])
    if name_node is None:
        return ""
    text = _text(source, name_node)
    # A C++ declarator is `add(int a, int b)`; the name is what precedes the parenthesis.
    return text.split("(")[0].strip() if "(" in text else text


def scan(root: str, package: str = "", version: str = "", *, language: str = "") -> list:
    """Every minable function under `root`, in the same shape the other scanners return.

    Deliberately the same contract as `functions.scan` and `javascript_functions.scan` -- the miner
    should not care which reader found a function.
    """
    spec = _GRAMMARS.get(language)
    if spec is None:
        return []
    parser = _parser(spec)
    if parser is None:
        return []

    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in spec["skip_dirs"]]
        for filename in filenames:
            if not filename.endswith(spec["suffixes"]):
                continue
            if spec["skip_files"] and filename.endswith(spec["skip_files"]):
                continue
            path = os.path.join(dirpath, filename)
            try:
                source = open(path, "rb").read()
            except OSError:
                continue
            # A generated or vendored megafile is not material a solver can reason about, and
            # parsing it costs more than it can return.
            if len(source) > 400_000:
                continue
            try:
                tree = parser.parse(source)
            except Exception:                               # noqa: BLE001 -- a grammar can reject
                continue
            module = os.path.relpath(path, root)
            for function in _find(tree.root_node, spec["functions"]):
                symbol = _symbol(function, spec, source)
                if not symbol or symbol.startswith("_"):
                    continue
                schema = _parameters(function, spec, source)
                # No parameters means nothing to sample, so no corpus can distinguish anything.
                if not schema:
                    continue
                found.append(SimpleNamespace(
                    package=package, version=version, module=module, symbol=symbol,
                    path=path, schema={"params": schema}, doc=""))
    found.sort(key=lambda item: (item.module, item.symbol))
    return found
