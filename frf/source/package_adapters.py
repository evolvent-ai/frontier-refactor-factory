"""Language-specific package surface adapters behind a language-neutral contract.

Adapters only inspect pinned source trees. They never import or execute them. The returned records are
plain JSON-compatible operation metadata consumed by the common PackageContract/call seam.
"""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Operation:
    name: str
    module: str
    symbol: str
    signature: str = ""
    language: str = ""
    json_safe: bool = True
    # The class owning a Ruby instance/static method. See `PackageOperation.klass` for why: a gem
    # surface is class methods, and a Ruby dispatcher has to instantiate the class before it can
    # reach one. Empty for a top-level `def` and for every language without this notion.
    klass: str = ""
    # TYPED ARGUMENTS for a static-language operation, as `native_functions` reads them: kind +
    # native spelling per parameter, and what the function returns. A generated static dispatcher
    # declares these; the regex `signature` string tells a generator but cannot be compiled from.
    params: tuple = ()
    result: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"name": self.name, "module": self.module, "symbol": self.symbol,
                "signature": self.signature, "language": self.language,
                "json_safe": self.json_safe, "klass": self.klass,
                "params": list(self.params), "result": dict(self.result)}


def operations(root: str, language: str, package_name: str, package_root: str) -> list[dict]:
    """Discover production operations for a supported package ecosystem."""
    language = (language or "").lower()
    adapter_name = _ADAPTER_NAMES.get(language)
    adapter = globals().get(adapter_name) if adapter_name else None
    return adapter(root, package_name, package_root) if adapter else []


_ADAPTER_NAMES = {
    "python": "_python", "javascript": "_javascript", "typescript": "_javascript",
    "rust": "_rust", "go": "_go", "ruby": "_ruby", "java": "_java",
    "c": "_c_cpp", "cpp": "_c_cpp",
}


def supported(language: str) -> bool:
    """Whether this module can discover a package surface for `language`.

    ONE truth for "which languages can be package subjects" on the SOURCING side. The gate in
    `github_package._inspect` used to keep its own list, and the two disagreed: the registry declared
    ruby's package scale and the adapter registered, while the source refused every ruby row as
    `unsupported-or-unpinned` before the adapter was ever consulted -- a 742-second package/ruby batch
    sourced zero candidates for that reason. A gate derived from this answer cannot drift from the
    adapter table, for the same reason `dispatch.languages()` is derived from its generators.
    """
    key = (language or "").strip().lower()
    return key in _ADAPTER_NAMES and globals().get(_ADAPTER_NAMES[key]) is not None


def _python(root, package_name, package_root):
    result, seen = [], set()
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in sorted(files):
            if not filename.endswith(".py") or filename.startswith("_") or _skip(filename):
                continue
            path = os.path.join(directory, filename)
            try:
                tree = ast.parse(open(path, encoding="utf-8", errors="replace").read(), path)
            except (OSError, SyntaxError, ValueError):
                continue
            rel = os.path.relpath(path, package_root)[:-3].replace(os.sep, ".")
            module = package_name + ("." + rel if rel != "__init__" else "")
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                    if node.name in seen:
                        continue
                    seen.add(node.name)
                    result.append(Operation(node.name, module, node.name, _signature(node), "python").to_json())
    return result


def _javascript(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if not filename.endswith((".js", ".mjs", ".cjs", ".ts")):
                continue
            text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
            # Store a filesystem-relative module path. Node's ESM loader cannot resolve the
            # Python-style dotted name that the first adapter emitted, especially for src/ trees.
            module = "./" + os.path.join(package_name,
                                          os.path.relpath(os.path.join(directory, filename), root)
                                          ).replace(os.sep, "/")
            names = []
            names.extend(re.findall(r"\bexport\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", text))
            names.extend(re.findall(r"\bexports\.([A-Za-z_$][\w$]*)\s*=", text))
            names.extend(re.findall(r"\bmodule\.exports\.([A-Za-z_$][\w$]*)\s*=", text))
            for block in re.findall(r"\bexport\s*\{([^}]*)\}", text):
                names.extend(re.findall(r"\b([A-Za-z_$][\w$]*)\b", block))
            for symbol in names:
                if symbol and not symbol.startswith("_"):
                    result.append(Operation(symbol, module, symbol, _js_signature(text, symbol), "javascript").to_json())
    return _unique(result)


def _rust(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if not filename.endswith(".rs"):
                continue
            text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
            module = package_name + "." + os.path.relpath(os.path.join(directory, filename), root)[:-3].replace(os.sep, ".")
            for match in re.finditer(r"\bpub\s+(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(", text):
                signature = _call_signature(text, match.end() - 1, returns=True)
                if signature is None:
                    continue
                symbol = match.group(1)
                result.append(Operation(symbol, module, symbol, signature, "rust").to_json())
    return _unique(result)


def _go(root, package_name, package_root):
    """Go package operations, typed by `native_functions` instead of a regex string.

    A static package dispatcher has to declare the real type for each argument -- `[]int`, `string`
    -- and `_call_signature` returns a STRING a dispatcher cannot generate from. `native_functions.
    scan` reads the same source with tree-sitter and emits `schema.params` (kind + native spelling),
    so the operation carries those beside the signature. The signature stays for the generator's
    benefit; `params`/`result` are what the dispatcher generator builds from.
    """
    from . import native_functions as native

    # THE GO IMPORT PATH, not a made-up dotted name. `_reactor` used to emit
    # `package_name + "." + os.path.relpath(...)` which produced `pkg..` -- a string with no
    # meaning to the Go compiler. A Go package dispatcher has to `import "github.com/x/y/graph"`
    # and call `graph.Func(...)`, so the operation's module is the real import path (go.mod module
    # plus the file's relative directory), and its klass is the declared package name (`graph`).
    module_path = ""
    go_mod_path = os.path.join(root, "go.mod")
    if os.path.isfile(go_mod_path):
        import re as _re
        match = _re.search(r"module\s+(\S+)", open(go_mod_path, encoding="utf-8", errors="replace").read())
        module_path = match.group(1) if match else ""

    result = []
    found = native.scan(root, package_name, "1.0", language="go")
    seen = set()
    for fn in found:
        # ONLY EXPORTED FUNCTIONS CROSS A PACKAGE BOUNDARY. Go hides a lowercase name inside its
        # own package, so a dispatcher importing `problem13` and calling `add` fails to compile:
        # `name add not exported by package problem13`. E2B reported eleven of these in one build,
        # and because a failed build's error text carries a per-run random path, freeze read the
        # five runs as five different answers and discarded 100% of probes as unstable material.
        if not fn.symbol[:1].isupper():
            continue
        # A VOID FUNCTION CANNOT BE THE VALUE OF AN EXPRESSION. `NextPermutation(x)` returns
        # nothing, and the generated `return NextPermutation(arg0), nil` is a compile error:
        # `(no value) used as value`. The package seam has no way to observe a Go mutation
        # (arguments arrive decoded from JSON, so a slice write is invisible to the caller), so
        # these are refused rather than mis-generated.
        if not (fn.result or {}):
            continue
        if fn.symbol in seen:
            continue
        seen.add(fn.symbol)
        # A FILE IN THE MODULE ROOT HAS NO SUBDIRECTORY, and `relpath` spells that "." -- which
        # concatenates into `github.com/gookit/goutil/.`, and Go refuses it: `malformed import
        # path: invalid path element "."`. The trailing-slash strip cannot catch it, because the
        # offending element is the dot rather than the separator. Repositories that expose their
        # API from the module root are ordinary, so this is not a rare shape.
        rel_dir = os.path.relpath(os.path.dirname(fn.path), root)
        suffix = "" if rel_dir in (".", "") else "/" + rel_dir.replace(os.sep, "/")
        import_path = (module_path + suffix) if module_path else ""
        signature = "(%s)" % ", ".join(str(p["native"]) for p in fn.schema["params"])
        result.append(Operation(fn.symbol, import_path, fn.symbol, signature, "go",
                                klass=fn.declared_package,
                                params=tuple(dict(p) for p in fn.schema["params"]),
                                result=dict(fn.result or {})).to_json())
    return _unique(result)


def _ruby(root, package_name, package_root):
    """Ruby gem surface: class instance methods, which is what a gem actually exposes.

    THE OLD REGULAR EXPRESSION MINED TOP-LEVEL `def`s -- and the real supply has none. Every gem
    surveyed (mightystring, geometry, google_distance_matrix, human_time, jekyll-spaceship, gush,
    perfect-shape) exposes its API as INSTANCE methods on a class: `MightyString::String#pop`, not
    `def pop` at the top. Mining top-level defs therefore produced either nothing (emptying the
    surface) or class methods that `send` cannot reach (raising NoMethodError on every probe, which
    freeze reads as 100% discarded).

    A RUBY INSTANCE METHOD NEEDS AN INSTANCE, so the operation carries the class that owns it --
    `klass` -- and only classes with a no-argument `initialize` are servable. That is the one shape a
    generated dispatcher can construct without guessing: `const_get(klass).new` takes no arguments
    and the method receives all of them. Three of the surveyed gems (human_time,
    google_distance_matrix, llm_docs_builder) expose exactly that shape, so the supply is real.

    `static` methods (`def self.x`) were kept too: they are sensible on the main object, but the gem
    convention puts them on named modules/classes, so they are recorded with their owning class and
    reached as `klass.method(...)`.
    """
    parser = None
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_ruby
        parser = Parser(Language(tree_sitter_ruby.language()))
    except Exception:                                   # noqa: BLE001 -- absent grammar
        return []

    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if not filename.endswith(".rb"):
                continue
            path = os.path.join(directory, filename)
            raw = open(path, encoding="utf-8", errors="replace").read()
            try:
                tree = parser.parse(raw.encode("utf-8"))
            except Exception:                            # noqa: BLE001 -- a grammar can reject
                continue
            rel_file = os.path.relpath(path, root)[:-3]
            module = rel_file.replace(os.sep, ".")

            def visit(node, klass):
                # RUBY SPELLS THE TWO KINDS DIFFERENTLY. An instance method is `method`; a static one
                # -- `def self.x` -- is `singleton_method`. The old traversal tested only `method`, so
                # human_time's four `def self.x` helpers on the HumanTime MODULE were _never reached_
                # and the surface mined as empty. Both kinds are servable; they just bind differently.
                if node.type in ("method", "singleton_method"):
                    name_node = node.child_by_field_name("name")
                    if name_node is None:
                        return
                    name = raw[name_node.start_byte:name_node.end_byte]
                    if name.startswith("_") or name == "initialize":
                        return
                    span = node.start_byte
                    # A STATIC node is static by its type; an instance `def` is not, and the receiver
                    # is only ever meaningful inside a class/module.
                    self_def = node.type == "singleton_method"
                    # `_call_signature` looks for a '(' after `span`; a parenless method -- `def
                    # self.greater_than_aliases`, which human_time's real surface is full of -- has
                    # none, so it returns None and the method is dropped. No parens means no
                    # parameters, which `"()"` says plainly.
                    open_paren = raw.find("(", span)
                    signature = (_call_signature(raw, open_paren) if open_paren != -1 else "()") or "()"
                    if klass:
                        # A STATIC method needs no instance -- `Klass.method(*args)` -- so it never
                        # requires `new`-ability. Only an INSTANCE method does, and only a class with
                        # a no-argument `initialize` is servable as one.
                        if not self_def and not _ruby_class_newable(tree, klass, raw):
                            return
                        symbol = ("self." + name if self_def else name)
                        result.append(Operation(name, module, symbol, signature,
                                                "ruby", klass=klass).to_json())
                    elif self_def:
                        # A top-level `def self.x` is not valid; skip.
                        return
                    else:
                        result.append(Operation(name, module, name, signature,
                                                "ruby").to_json())
                for child in node.children:
                    if child.type in ("class", "module"):
                        name_node = _ruby_node_name(child)
                        if name_node is None:
                            continue
                        sub = raw[name_node.start_byte:name_node.end_byte]
                        qualified = "%s::%s" % (klass, sub) if klass else sub
                        visit(child, qualified)
                    elif child.type == "body_statement":
                        for c in child.children:
                            visit(c, klass)
                    elif klass is None and child.type in ("method", "singleton_method"):
                        visit(child, klass)

            visit(tree.root_node, None)
    return _unique(result)


def _ruby_node_name(node) -> object | None:
    """The NAME child of a class/module node, found without relying on the grammar's field.

    tree-sitter's Ruby grammar gives some (but not all) `class`/`module` nodes a usable `name`
    field. A `module HumanTime; module String` nesting returns None for the second one, which is
    where the old `child_by_field_name("name")` died mid-walk and silently dropped the remaining
    methods of the file. The name is always the first `constant` or `identifier` child of the
    declaration; children[0] is the keyword node, so the search starts at index 1.
    """
    for child in node.children[1:]:
        if child.type in ("constant", "identifier", "scope_resolution", "constant_path"):
            return child
    return None


def _ruby_class_newable(tree, klass: str, raw: str) -> bool:
    """Whether `klass` can be constructed with no arguments.

    `initialize` declared with no parameters is the one shape a dispatcher can call without guessing.
    A class whose initialize takes arguments -- `Arc#initialize(center, radius, ...)`, which the
    surveyed geometry gem is full of -- would need arguments the probe protocol has no place for, so
    it is not servable and its instance methods are skipped rather than called wrongly.
    """
    const = klass.split("::")
    node = tree.root_node
    for part in const:
        found = None
        stack = [node]
        while stack and found is None:
            current = stack.pop()
            if current.type in ("class", "module"):
                name = _ruby_node_name(current)
                if name is not None and raw[name.start_byte:name.end_byte] == part:
                    found = current
                    break
            stack.extend(current.children)
        if found is None:
            return False
        node = found
    # We found the class; now search its body for a no-arg initialize.
    newable = None
    for child in node.children:
        if child.type == "body_statement":
            for c in child.children:
                if c.type == "method":
                    name = c.child_by_field_name("name")
                    if name is not None and raw[name.start_byte:name.end_byte] == "initialize":
                        params = c.child_by_field_name("parameters")
                        if params is None or all(p.type in ("(", ")") for p in params.children):
                            newable = True
                        else:
                            return False
    return bool(newable)


def _java(root, package_name, package_root):
    """Java package operations, typed and class-owned via `native_functions`.

    Same change as `_go`: a generated static dispatcher needs a concrete type per argument and the
    class that owns the method, which the regex `signature` string cannot give. `native_functions`
    reads those from the grammar.
    """
    from . import native_functions as native

    result = []
    found = native.scan(root, package_name, "1.0", language="java")
    seen = set()
    for fn in found:
        if fn.symbol in seen:
            continue
        seen.add(fn.symbol)
        module = package_name + "." + os.path.relpath(os.path.dirname(fn.path), root).replace(os.sep, ".")
        signature = "(%s)" % ", ".join(str(p["native"]) for p in fn.schema["params"])
        result.append(Operation(fn.symbol, module, fn.symbol, signature, "java",
                                klass=fn.owner,
                                params=tuple(dict(p) for p in fn.schema["params"]),
                                result=dict(fn.result or {})).to_json())
    return _unique(result)


def _c_cpp(root, package_name, package_root):
    """C/C++ package operations, typed via `native_functions` like Go and Java."""
    from . import native_functions as native

    result = []
    found = native.scan(root, package_name, "1.0", language="cpp")
    seen = set()
    for fn in found:
        if fn.symbol in seen:
            continue
        seen.add(fn.symbol)
        module = package_name + "." + os.path.relpath(os.path.dirname(fn.path), root).replace(os.sep, ".")
        signature = "(%s)" % ", ".join(str(p["native"]) for p in fn.schema["params"])
        result.append(Operation(fn.symbol, module, fn.symbol, signature, "cpp",
                                params=tuple(dict(p) for p in fn.schema["params"]),
                                result=dict(fn.result or {})).to_json())
    return _unique(result)


def _unique(items):
    seen = set()
    return [item for item in items if not (item["name"] in seen or seen.add(item["name"]))]


def _signature(node):
    return "(" + ", ".join(arg.arg for arg in list(node.args.posonlyargs) + list(node.args.args)) + ")"


def _call_signature(text, open_index, returns=False, limit=800):
    """The signature source beginning at the '(' at open_index, whitespace collapsed.

    Parameter lists never contain unbalanced parentheses, so plain paren counting
    survives generics (Map<String, List<Integer>>) and signatures broken across
    lines alike. With returns=True the type written after the closing paren is
    appended, which is where Rust (-> T) and Go (T, or (T, error)) record it.
    Returns None when the list is unterminated inside the scan window, so a
    caller can decline the symbol rather than invent a signature for it.
    """
    close_index = _close_paren(text, open_index, limit)
    if close_index is None:
        return None
    params = " ".join(text[open_index:close_index + 1].split())
    if not returns:
        return params
    tail = text[close_index + 1:close_index + 121]
    cut = min((at for at in (tail.find("{"), tail.find(";"), tail.find("\n")) if at != -1), default=len(tail))
    return (params + " " + " ".join(tail[:cut].split())).strip()


def _js_signature(text, symbol):
    """The parameter list declared for symbol in JavaScript or TypeScript source, or "".

    Only two of the four export forms carry parens at the export site, so the
    declaration is located by name instead. An empty string keeps the behaviour
    these adapters had before signatures were recorded: the name still names a
    real export, the model simply reads the source to learn its arguments.
    """
    name = re.escape(symbol)
    for pattern in (r"\bfunction\s*\*?\s*" + name + r"\s*(?=\()",
                    r"\b" + name + r"\s*[:=]\s*(?:async\s+)?function\s*\*?\s*(?=\()",
                    r"\b" + name + r"\s*[:=]\s*(?:async\s+)?(?=\()",
                    r"^[ \t]*(?:async\s+)?" + name + r"\s*(?=\()"):
        match = re.search(pattern, text, re.M)
        if match:
            signature = _call_signature(text, match.end())
            if signature is not None:
                return signature
    return ""


def _close_paren(text, open_index, limit=800):
    """The index of the ')' closing the '(' at open_index, or None if unterminated."""
    if open_index >= len(text) or text[open_index] != "(":
        return None
    depth = 0
    for position in range(open_index, min(len(text), open_index + limit)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return position
    return None


def _skip(name):
    return name in {"tests", "test", "testing", "docs", "examples", "bench", "benchmarks", "fixtures", "testdata", "conftest.py"} or name.startswith(".")
