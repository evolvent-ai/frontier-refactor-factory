"""Language-specific package surface adapters behind a language-neutral contract.

Adapters only inspect pinned source trees. They never import or execute them. The returned records are
plain JSON-compatible operation metadata consumed by the common PackageContract/call seam.
"""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    name: str
    module: str
    symbol: str
    signature: str = ""
    language: str = ""
    json_safe: bool = True

    def to_json(self) -> dict:
        return {"name": self.name, "module": self.module, "symbol": self.symbol,
                "signature": self.signature, "language": self.language,
                "json_safe": self.json_safe}


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
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if not filename.endswith(".go") or filename.endswith("_test.go"):
                continue
            text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
            module = package_name + "." + os.path.relpath(directory, root).replace(os.sep, ".")
            for match in re.finditer(r"\bfunc\s+([A-Z]\w*)\s*\(", text):
                signature = _call_signature(text, match.end() - 1, returns=True)
                if signature is None:
                    continue
                symbol = match.group(1)
                result.append(Operation(symbol, module, symbol, signature, "go").to_json())
    return _unique(result)


def _ruby(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if filename.endswith(".rb"):
                text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
                # RELATIVE TO THE REPO ROOT, WITH NO PACKAGE-NAME PREFIX. The dispatcher's
                # `require_relative` resolves against the directory holding subject.rb, which is the
                # room root after `_serve_package_here`'s copytree of `material.root` -- so the path
                # the runtime sees is `<room>/lib/algo/sorting.rb`, and the module has to be
                # `lib.algo.sorting` for `require_relative "lib/algo/sorting"` to find it.
                #
                # Prefixed with the package name -- `algo.lib.algo.sorting` -- the require path became
                # `algo/lib/algo/sorting`, which `_serve_package_here` never creates: copytree only
                # puts `material.root`'s own subtree there, and `lib/` is already inside it. Every
                # ruby package task would have failed E7 with LoadError after passing every earlier
                # gate. Because a ruby gem has no import-time package prefix -- the load path is the
                # directory -- the correct module is the file path, not a dotted namespace.
                rel_file = os.path.relpath(os.path.join(directory, filename), root)[:-3]
                module = rel_file.replace(os.sep, ".")
                for match in re.finditer(r"^[ \t]*def\s+([a-zA-Z_]\w*[!?=]?)", text, re.M):
                    symbol = match.group(1)
                    if symbol.startswith("_"):
                        continue
                    rest = text[match.end():]
                    if rest[:1] == "(":
                        signature = _call_signature(text, match.end())
                    elif rest.split("\n", 1)[0].strip():
                        continue  # parenless arguments; the name alone would misstate the arity
                    else:
                        signature = "()"
                    if signature is None:
                        continue
                    result.append(Operation(symbol, module, symbol, signature, "ruby").to_json())
    return _unique(result)


def _java(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if filename.endswith(".java"):
                text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
                module = package_name + "." + os.path.relpath(os.path.join(directory, filename), root)[:-5].replace(os.sep, ".")
                for match in re.finditer(r"\bpublic\s+(?:static\s+)?([\w<>\[\], ]+?)\s+([A-Za-z_]\w*)\s*\(", text):
                    params = _call_signature(text, match.end() - 1)
                    if params is None:
                        continue
                    returns, symbol = " ".join(match.group(1).split()), match.group(2)
                    result.append(Operation(symbol, module, symbol, params + " -> " + returns, "java").to_json())
    return _unique(result)


def _c_cpp(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if filename.endswith((".h", ".hpp")):
                text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
                module = package_name + "." + os.path.relpath(os.path.join(directory, filename), root)
                for match in re.finditer(r"\b((?:[A-Za-z_]\w*[\s*&]+)+)([A-Za-z_]\w*)\s*(?=\()", text):
                    close_index = _close_paren(text, match.end())
                    if close_index is None or text[close_index + 1:].lstrip()[:1] != ";":
                        continue  # a definition or macro body, not a declared entry point
                    params = " ".join(text[match.end():close_index + 1].split())
                    returns, symbol = " ".join(match.group(1).split()), match.group(2)
                    result.append(Operation(symbol, module, symbol, params + " -> " + returns, "cpp").to_json())
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
