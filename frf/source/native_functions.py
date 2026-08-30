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

# WHICH KINDS CAN CARRY A MUTATION, taken from the vocabulary that defines them rather than restated.
# A void function is observed through what it writes into its arguments, so this file needs to know
# which of them are writable -- and a second copy of that tuple would be a second thing to update
# when a kind is added.
from ..observe.probes.schema import ARRAY_KINDS

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
        # Where this language writes what a function GIVES BACK. A probe schema does not need it --
        # the corpus only describes inputs -- but the generated bridge does: it has to declare a
        # variable of the real type to hold the result, and Go says nothing at all where a function
        # returns nothing. An absent field is void, which is a third of the supply, not an edge case.
        "result_field": "result",
        # The declaration a generated bridge has to agree with, when the language has one.
        "package_node": "package_clause",
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
        "result_field": "return_type",
        # Rust has no package clause a bridge must match: the shim reaches the subject as
        # `mod subject`, so the file's own name is the declaration.
        "package_node": "",
        # UNREACHABLE BY ITS PLAIN NAME. A bridge calls the mined function as `symbol(args)`, so an
        # associated function inside `impl T` needs `T::symbol` and one inside `mod inner` needs
        # `inner::symbol`. Neither carries a `self_parameter`, so the receiver refusal does not catch
        # them, and a bridge generated for one does not compile -- charged to the material.
        "skip_nested_in": ("impl_item", "mod_item", "trait_item"),
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
        # Java spells a method's return type in the same `type` field a parameter uses, and says
        # `void` explicitly rather than leaving it out.
        "result_field": "type",
        # The bridge is generated as class `Subject`, which the shim reflects for; a package
        # declaration in the mined file would put the real symbol somewhere else, so it is read.
        "package_node": "package_declaration",
        # EVERY JAVA METHOD IS IN A CLASS, so nesting cannot be the test -- reachability is. A bridge
        # calls `Owner.method(...)`, which needs no instance only when the method is static. An
        # instance method would need a constructor this factory cannot know how to call, and the
        # generated bridge would not compile.
        "require_static": True,
        # AND REACHABLE FROM ANOTHER CLASS, which `static` alone does not make it. The bridge is
        # generated as class `Subject` and calls `Owner.method(...)`; a `private static` method
        # satisfies the check above and still refuses to compile -- `getLCA(int,int,int[],int[])
        # has private access in LCA`. Two of TheAlgorithms/Java's candidates failed exactly that
        # way in a real batch, charged to the material, and java has never emitted a task.
        #
        # This is Go's exported-name rule wearing Java's spelling: the miner must not offer a
        # symbol the generated caller is not allowed to name. Package-private (no modifier at all)
        # is refused for the same reason -- `Subject` carries the mined file's own package
        # declaration only when it has one, so same-package access cannot be relied on.
        "require_public": True,
        # Which class owns the method, so the bridge can name it.
        "owner_nodes": ("class_declaration",),
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
        # C++ puts the return type in the same `type` field as a parameter's, and spells `void`.
        "result_field": "type",
        # A namespace, if the file opens one, is part of the name the bridge has to call.
        "package_node": "namespace_definition",
        # A member function needs an instance, and a namespaced one needs qualifying -- neither is
        # callable as `symbol(args)`, which is what the bridge emits. The C shim also reaches the
        # subject through `extern "C"`, so only a free function at file scope can be bound.
        "skip_nested_in": ("class_specifier", "struct_specifier", "namespace_definition"),
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

    Each entry also carries `native`: the parameter's type AS THE SOURCE SPELLS IT. The schema kind
    says what a probe may DRAW, and several spellings collapse onto one kind -- `[]int`, `[]int32` and
    `[]int64` are all `int_array`. A generated bridge has to declare the real type to call the real
    function, so the spelling cannot be recovered from the kind and is kept beside it. `scan` splits
    the two apart; `Param.from_json` would drop this key anyway.
    """
    holder = function.child_by_field_name(spec["params_field"])
    if holder is None:
        return None
    # A METHOD IS NOT CALLABLE WITHOUT ITS RECEIVER, and nothing here can construct one. Rust hides
    # this the worst: `self_parameter` is not in `param_nodes`, so `fn add(&mut self, cost: f64)` was
    # mined as a free function of one argument -- a bridge generated from that cannot compile, and the
    # failure would be charged to the material. Go marks it with a `receiver` field; both are refused.
    if any(child.type == "self_parameter" for child in holder.children):
        return None
    params = _find(holder, spec["param_nodes"])
    schema = []
    for param in params:
        type_node = param.child_by_field_name(spec["type_field"])
        if type_node is None:
            return None
        spelling = _text(source, type_node)
        described = _param_schema(spelling, spec["types"])
        if described is None:
            return None
        # ONE DECLARATION CAN NAME SEVERAL PARAMETERS. Go writes
        # `func K(maxWeight int, weights, values []int)` -- three parameters in two declarations,
        # because `weights, values []int` shares one type between two names. Reading only the first
        # name produced a schema of two for a function of three, so the generated bridge called it
        # with the wrong arity: `not enough arguments in call to Knapsack, have (int, []int)`. That
        # build failure was then charged to the MATERIAL, which is precisely the outcome the
        # whole-function refusal above exists to prevent, reached by another route.
        #
        # `children_by_field_name` rather than the singular form, which returns only the first. The
        # other grammars repeat the type per parameter and so return a list of one.
        names = list(param.children_by_field_name(spec["param_name_field"])) or [None]
        for name_node in names:
            entry = dict(described)
            # A C++ declarator is `&s` or `*p`; the sigil belongs to the type, not the name.
            entry["name"] = (_text(source, name_node).lstrip("&*").strip()
                             if name_node is not None else "") or "arg%d" % len(schema)
            entry["native"] = " ".join(spelling.split())
            schema.append(entry)
    return schema


def _result(function, spec: dict, source: bytes) -> dict | None:
    """What the function gives back: `{}` for void, a described type, or None if it cannot cross.

    NOT PART OF THE PROBE SCHEMA, which describes inputs only. It is needed the moment a bridge has
    to declare a variable to hold the answer and hand it to a JSON encoder.

    Described with the SAME table the parameters use, so that "which spellings this factory can carry"
    has one definition. The bridge then only needs to know how to turn a KIND into code -- were it to
    re-derive kinds from spellings, that would be the second copy of a mapping this file already owns,
    and the second copy is the one that goes stale.

    VOID IS `{}` AND IS NOT AN ERROR. About a third of the Go functions in the checkouts on hand
    return nothing, because sorting an array in place is how a lot of real code is written -- and an
    in-place sort over a drawable array is exactly what the kernel scale wants. Go says so by omitting
    the field; Java and C++ spell `void`. Whether such a function can be OBSERVED is a further
    question, answered in `scan`: the mutation of an argument is the answer, so some argument has to
    be able to carry one.

    None means the function returns something real that this wire cannot express -- a Rust
    `AppxDbscanParams<F, CommonNearestNeighbour>`, a builder, a handle. Refused whole, for the same
    reason an untypeable parameter is: a value that cannot be encoded would arrive at the comparator
    as a failure charged to the candidate.
    """
    field = spec.get("result_field") or ""
    if not field:
        return {}
    node = function.child_by_field_name(field)
    if node is None:
        return {}
    spelling = " ".join(_text(source, node).split())
    if spelling in ("void", ""):
        return {}
    described = _param_schema(spelling, spec["types"])
    if described is None:
        return None
    described["native"] = spelling
    return described


def _declared_package(tree, spec: dict, source: bytes) -> str:
    """The package or namespace the mined file declares, or "" when it declares none.

    A generated bridge has to AGREE with this. The first Go batch refused every candidate with
    `found packages main (serve.go) and dynamic (subject.go)`: the shim is `package main` and the
    material was `package dynamic`, and nothing had looked at the second name in order to reconcile
    them.
    """
    wanted = spec.get("package_node") or ""
    if not wanted:
        return ""
    for node in _find(tree.root_node, (wanted,)):
        name = node.child_by_field_name("name")
        if name is not None:
            return _text(source, name).strip()
        # Go's package_clause has no named field: the text is `package dynamic`.
        text = _text(source, node).strip().rstrip("{").strip()
        return text.split()[-1] if text.split() else ""
    return ""


def _reachable(function, spec: dict, source: bytes) -> bool:
    """Whether a bridge can call this function by name, with no instance and no qualification.

    THE SAME CLASS OF FAULT AS A RECEIVER, arriving without one. A bridge emits `symbol(args)`, so:

        Rust  -- `impl T { fn assoc(a: i64) }` needs `T::assoc`, `mod inner { fn f() }` needs
                 `inner::f`. Neither has a `self_parameter`, so the receiver check above passes them
                 straight through, and the generated bridge fails to compile.
        C++   -- a member function needs an instance and a namespaced one needs qualifying; the C shim
                 also reaches the subject through `extern "C"`, which a member cannot be.
        Java  -- every method is inside a class, so nesting cannot be the test. `Owner.method(...)`
                 needs no instance only when the method is static; an instance method would need a
                 constructor this factory has no way to call.

    Each of those is a build failure charged to the MATERIAL, which is what the whole-function
    refusals in this file exist to prevent.
    """
    forbidden = spec.get("skip_nested_in") or ()
    if forbidden:
        parent = function.parent
        while parent is not None:
            if parent.type in forbidden:
                return False
            parent = parent.parent
    if spec.get("require_static") or spec.get("require_public"):
        modifiers = next((child for child in function.children if child.type == "modifiers"), None)
        words = _text(source, modifiers).split() if modifiers is not None else []
        if spec.get("require_static") and "static" not in words:
            return False
        # `public` is required EXPLICITLY rather than inferred from the absence of `private`.
        # Java's default is package-private, which is a third state that reads like public in the
        # source and compiles like private from `Subject`.
        if spec.get("require_public") and "public" not in words:
            return False
    return True


def _owner(function, spec: dict, source: bytes) -> str:
    """The class a static method belongs to, so a bridge can call `Owner.method(...)`. "" if none."""
    for wanted in spec.get("owner_nodes") or ():
        parent = function.parent
        while parent is not None:
            if parent.type == wanted:
                name = parent.child_by_field_name("name")
                return _text(source, name).strip() if name is not None else ""
            parent = parent.parent
    return ""


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

    TWO FIELDS BEYOND THAT SHAPE, and only a bridge generator reads them: `result` (what the function
    gives back, as the source spells it, "" for void) and `declared_package` (the package or namespace
    the file opens). Neither describes an input, so neither belongs in the probe schema; both are
    required to generate code that COMPILES against the real symbol. The dynamic readers do not set
    them because their shims need no bridge, so a caller reads them with a default.
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
            # Read once per FILE, not per function: a package clause is a property of the file.
            declared = _declared_package(tree, spec, source)
            for function in _find(tree.root_node, spec["functions"]):
                # A METHOD NEEDS A RECEIVER and nothing here can build one. Go marks it with a
                # `receiver` field; Rust's `self_parameter` is caught in `_parameters`.
                if function.child_by_field_name("receiver") is not None:
                    continue
                # And a function a bridge cannot NAME is refused for the same reason -- see
                # `_reachable`. This catches what the receiver check cannot: an `impl` block, a `mod`,
                # a C++ member, a non-static Java method.
                if not _reachable(function, spec, source):
                    continue
                symbol = _symbol(function, spec, source)
                # A QUALIFIED NAME IS A DEFINITION OF SOMETHING DECLARED ELSEWHERE, and it sits at
                # FILE SCOPE, so the nesting check above cannot see it. C++ writes a member's body as
                # `void PyramidCU::BuildPyramid(int n) { ... }` outside the class, and a bridge
                # emitting `BuildPyramid(arg0)` does not compile -- `'PyramidCU' has not been
                # declared` was one of six real build refusals in a kernel/cpp batch. `Outer::Inner::
                # deep` was mined whole, qualifier and all, which no caller could name either.
                if "::" in symbol:
                    continue
                if not symbol or symbol.startswith("_"):
                    continue
                schema = _parameters(function, spec, source)
                # No parameters means nothing to sample, so no corpus can distinguish anything.
                if not schema:
                    continue
                result = _result(function, spec, source)
                # Returns something this wire cannot carry. See `_result`.
                if result is None:
                    continue
                # A VOID FUNCTION IS OBSERVED THROUGH WHAT IT MUTATES, so there has to be something
                # mutable to observe. An in-place sort taking `[]int` is ideal material; a void
                # function of scalars only has copied its arguments and left no evidence anywhere, so
                # every probe would return the same nothing and no corpus could distinguish an
                # implementation from a stub.
                if not result and not any(p["kind"] in ARRAY_KINDS for p in schema):
                    continue
                found.append(SimpleNamespace(
                    package=package, version=version, module=module, symbol=symbol,
                    path=path, schema={"params": schema}, doc="",
                    result=result, declared_package=declared,
                    # Which class holds a static method, so a Java bridge can call `Owner.method(...)`.
                    # "" for the languages whose functions stand at file scope.
                    owner=_owner(function, spec, source)))
    found.sort(key=lambda item: (item.module, item.symbol))
    return found
