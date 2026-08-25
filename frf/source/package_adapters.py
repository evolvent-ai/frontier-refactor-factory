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
    if language == "python":
        return _python(root, package_name, package_root)
    if language in ("javascript", "typescript"):
        return _javascript(root, package_name, package_root)
    if language == "rust":
        return _rust(root, package_name, package_root)
    if language == "go":
        return _go(root, package_name, package_root)
    if language == "ruby":
        return _ruby(root, package_name, package_root)
    if language == "java":
        return _java(root, package_name, package_root)
    if language in ("c", "cpp"):
        return _c_cpp(root, package_name, package_root)
    return []


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
            module = package_name + "." + os.path.relpath(os.path.join(directory, filename), root)
            module = re.sub(r"\.(mjs|cjs|js|ts)$", "", module).replace(os.sep, ".")
            names = []
            names.extend(re.findall(r"\bexport\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", text))
            names.extend(re.findall(r"\bexports\.([A-Za-z_$][\w$]*)\s*=", text))
            names.extend(re.findall(r"\bmodule\.exports\.([A-Za-z_$][\w$]*)\s*=", text))
            for block in re.findall(r"\bexport\s*\{([^}]*)\}", text):
                names.extend(re.findall(r"\b([A-Za-z_$][\w$]*)\b", block))
            for symbol in names:
                if symbol and not symbol.startswith("_"):
                    result.append(Operation(symbol, module, symbol, language="javascript").to_json())
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
            for symbol in re.findall(r"\bpub\s+(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(", text):
                result.append(Operation(symbol, module, symbol, language="rust").to_json())
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
            for symbol in re.findall(r"\bfunc\s+([A-Z]\w*)\s*\(", text):
                result.append(Operation(symbol, module, symbol, language="go").to_json())
    return _unique(result)


def _ruby(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if filename.endswith(".rb"):
                text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
                module = package_name + "." + os.path.relpath(os.path.join(directory, filename), root)[:-3].replace(os.sep, ".")
                for symbol in re.findall(r"^\s*def\s+([a-zA-Z_]\w*[!?=]?)", text, re.M):
                    if not symbol.startswith("_"):
                        result.append(Operation(symbol, module, symbol, language="ruby").to_json())
    return _unique(result)


def _java(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if filename.endswith(".java"):
                text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
                module = package_name + "." + os.path.relpath(os.path.join(directory, filename), root)[:-5].replace(os.sep, ".")
                for symbol in re.findall(r"\bpublic\s+(?:static\s+)?[\w<>\[\]]+\s+([A-Za-z_]\w*)\s*\(", text):
                    result.append(Operation(symbol, module, symbol, language="java").to_json())
    return _unique(result)


def _c_cpp(root, package_name, package_root):
    result = []
    for directory, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if not _skip(d)]
        for filename in files:
            if filename.endswith((".h", ".hpp")):
                text = open(os.path.join(directory, filename), encoding="utf-8", errors="replace").read()
                module = package_name + "." + os.path.relpath(os.path.join(directory, filename), root)
                for symbol in re.findall(r"\b(?:[A-Za-z_]\w*[\s*&]+)+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*;", text):
                    result.append(Operation(symbol, module, symbol, language="cpp").to_json())
    return _unique(result)


def _unique(items):
    seen = set()
    return [item for item in items if not (item["name"] in seen or seen.add(item["name"]))]


def _signature(node):
    return "(" + ", ".join(arg.arg for arg in list(node.args.posonlyargs) + list(node.args.args)) + ")"


def _skip(name):
    return name in {"tests", "test", "testing", "docs", "examples", "bench", "benchmarks", "fixtures", "testdata", "conftest.py"} or name.startswith(".")
