"""Static discovery of JSON-safe top-level JavaScript/TypeScript functions.

This adapter deliberately accepts only explicit types: TypeScript annotations or JSDoc ``@param``
tags. Untyped JavaScript is not guessed, because a guessed schema would make a valid-looking task
that mostly exercises error paths. The returned objects use the same small shape as the Python
function miner and are consumed by the existing module/kernel scales.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace


_PARAM = re.compile(r"@param\s+\{([^}]+)\}\s+(?:\[)?([A-Za-z_$][\w$]*)(?:\])?")
_FUNCTION = re.compile(
    r"(?m)^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*(?:\:\s*[^\{]+)?\{"
)
_ARROW = re.compile(
    r"(?m)^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*(?::\s*[^=]+)?=>\s*\{"
)
_TYPES = {
    "number": {"kind": "float"}, "string": {"kind": "string", "size": "n"},
    "boolean": {"kind": "bool"}, "bigint": {"kind": "int", "low": -1000, "high": 1000},
}


def _type_schema(type_name: str) -> dict | None:
    value = type_name.strip().lower().replace("readonly ", "")
    if value.endswith("[]"):
        base = value[:-2]
        if base == "number":
            return {"kind": "float_array", "dtype": "float64", "size": "n"}
        if base in ("int", "integer", "bigint"):
            return {"kind": "int_array", "dtype": "int64", "size": "n"}
        return None
    if value.startswith("array<") and value.endswith(">"):
        return _type_schema(value[6:-1] + "[]")
    item = _TYPES.get(value)
    return dict(item) if item else None


def _params(raw: str, jsdoc: str) -> list[dict] | None:
    pieces = [p.strip() for p in raw.split(",")] if raw.strip() else []
    if not pieces or len(pieces) > 4 or any(p.startswith("...") for p in pieces):
        return None
    docs = {name: typ for typ, name in _PARAM.findall(jsdoc)}
    result = []
    for piece in pieces:
        match = re.match(r"([A-Za-z_$][\w$]*)\s*(?:\?\s*)?(?::\s*([^=]+))?", piece)
        if not match:
            return None
        name, annotation = match.groups()
        schema = _type_schema(annotation or docs.get(name, ""))
        if schema is None:
            return None
        result.append(schema)
    return result


def scan(root: str, package: str = "", version: str = "") -> list:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".git", "dist", "build"}]
        for filename in filenames:
            if not filename.endswith((".js", ".mjs", ".cjs", ".ts")):
                continue
            path = os.path.join(dirpath, filename)
            try:
                source = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if re.search(r"(?m)^\s*import\s+|require\s*\(", source):
                continue
            module = os.path.relpath(path, root).replace(os.sep, "/")
            module = re.sub(r"\.(m?js|cjs|ts)$", "", module)
            matches = list(_FUNCTION.finditer(source)) + list(_ARROW.finditer(source))
            for match in matches:
                # Regex is used only after this lexical guard. Indented declarations are usually
                # nested helpers, not a standalone export the shim can import reliably.
                if source[:match.start()].count("{") - source[:match.start()].count("}") != 0:
                    continue
                symbol, raw = match.groups()
                if symbol.startswith("_"):
                    continue
                prefix = source[max(0, match.start() - 1200):match.start()]
                jsdoc_match = re.search(r"/\*\*.*?\*/\s*$", prefix, re.S)
                schema = _params(raw, jsdoc_match.group(0) if jsdoc_match else "")
                if schema is None:
                    continue
                body = source[match.end():]
                if not any(token in body[:4000] for token in (".map(", ".reduce(", ".sort(",
                                                               "for (", "for(", "while (", "while(")):
                    kinds = {param["kind"] for param in schema}
                    if not kinds & {"string", "float_array", "int_array"}:
                        continue
                found.append(SimpleNamespace(package=package, version=version, module=module,
                    symbol=symbol, path=path, schema={"params": schema}, doc=""))
    found.sort(key=lambda item: (item.module, item.symbol))
    return found
