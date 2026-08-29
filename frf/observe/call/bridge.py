"""The third part of the call seam: binding a mined function to a shim that cannot find it.

WHY THIS FILE EXISTS. A call seam needs a miner, a shim, and something to join them. The dynamic
languages hide the third part by closing it at run time -- `serve.py` does
`getattr(import_module(mod), symbol)` and then `_entry(*args)`, `serve.js` indexes `subject[symbol]`
-- so handed any function the miner found, under its own name and arity, they serve it.

A static shim cannot. `serve.go` requires `func Entry(args []interface{}) (interface{}, error)` in
`package main`. Real mined material is `func BubbleSort(array []int) []int` in `package sort`. With a
miner and a shim both present and nothing between them, the first Go kernel batch refused every
candidate at build with `found packages main (serve.go) and sort (subject.go)`, and `undefined: Entry`
once the package name was fixed by hand. Neither message is about the material.

WHAT A BRIDGE IS. One generated file per candidate that declares exactly what the shim expects,
converts the JSON argument list into concrete typed values, calls the real symbol, and hands back
something the wire can encode. It is generated rather than templated because the types are per
candidate: `[]int` and `[]float64` need different conversion code, and no fixed template covers both.

WHY IT GENERATES FROM KINDS, NOT SPELLINGS. `source/native_functions.py` owns the map from a source
type spelling to what the wire can carry, and it keeps the spelling beside the kind for exactly this
file to use. So a bridge asks "what kind is this, and what does the source call it" and never re-parses
a type. A second copy of that mapping is the one that goes stale.

WHAT IS DELIBERATELY REFUSED. A language with no generator raises, loudly, rather than being handed a
bridge in the wrong language -- the same rule `dispatch.py` follows, and for the same reason: the
alternative is a build failure that reads as though the material were broken.
"""
from __future__ import annotations


class Unsupported(RuntimeError):
    """Raised when a language has no bridge generator yet."""


# ----------------------------------------------------------------------------------- Go
#
# Only the converters a bridge actually uses are emitted: an unused function is legal in Go, but an
# unused IMPORT is a compile error, and the two are easy to conflate when editing this table.
_GO_CONVERTERS = {
    "int": ("frfInt", """
func frfInt(value interface{}) (int, bool) {
	// JSON has one number type and Go's decoder hands it over as float64, so every integer
	// argument arrives needing this. A non-integral float is REFUSED rather than truncated:
	// silently dropping .5 would make the subject answer a question it was not asked.
	number, ok := value.(float64)
	if !ok || number != float64(int(number)) {
		return 0, false
	}
	return int(number), true
}
"""),
    "float": ("frfFloat", """
func frfFloat(value interface{}) (float64, bool) {
	number, ok := value.(float64)
	return number, ok
}
"""),
    "bool": ("frfBool", """
func frfBool(value interface{}) (bool, bool) {
	flag, ok := value.(bool)
	return flag, ok
}
"""),
    "string": ("frfString", """
func frfString(value interface{}) (string, bool) {
	text, ok := value.(string)
	return text, ok
}
"""),
    "int_array": ("frfInts", """
func frfInts(value interface{}) ([]int, bool) {
	// A nil slice and an empty one are different values to a subject that checks len() against
	// nil, so an empty JSON array becomes an allocated slice of length zero rather than nil.
	items, ok := value.([]interface{})
	if !ok {
		return nil, false
	}
	out := make([]int, 0, len(items))
	for _, item := range items {
		converted, ok := frfInt(item)
		if !ok {
			return nil, false
		}
		out = append(out, converted)
	}
	return out, true
}
"""),
    "float_array": ("frfFloats", """
func frfFloats(value interface{}) ([]float64, bool) {
	items, ok := value.([]interface{})
	if !ok {
		return nil, false
	}
	out := make([]float64, 0, len(items))
	for _, item := range items {
		converted, ok := frfFloat(item)
		if !ok {
			return nil, false
		}
		out = append(out, converted)
	}
	return out, true
}
"""),
}

# What a converted value must be declared as, when the source spelling is not the converter's own
# type. `frfInt` yields `int`; a parameter spelled `int32` needs the conversion written out.
_GO_NATIVE_OF_KIND = {
    "int": "int", "float": "float64", "bool": "bool", "string": "string",
    "int_array": "[]int", "float_array": "[]float64",
}


def _go(symbol: str, params: list, result: dict, package: str) -> str:
    lines = ["package main", "", 'import "fmt"', ""]
    used: list = []
    body = []
    call_args = []

    for index, param in enumerate(params):
        kind = str(param.get("kind", ""))
        entry = _GO_CONVERTERS.get(kind)
        if entry is None:
            raise Unsupported(
                "no Go conversion for a %r argument; the bridge would not compile" % kind)
        converter, _ = entry
        if kind not in used:
            used.append(kind)
        native = " ".join(str(param.get("native", "")).split()) or _GO_NATIVE_OF_KIND[kind]
        name = "arg%d" % index
        body.append("\t%s, ok%d := %s(args[%d])" % (name, index, converter, index))
        body.append("\tif !ok%d {" % index)
        body.append('\t\treturn nil, fmt.Errorf("argument %d is not a %s")' % (index, kind))
        body.append("\t}")
        # THE SPELLING MATTERS, NOT ONLY THE KIND. `[]int32` and `[]int` are both `int_array`, and a
        # converter yields `[]int`; handing that straight to a function declared `[]int32` does not
        # compile. The conversion is written only when the two differ, because `[]int([]int)` is
        # legal but noisy and `int(int)` reads like a mistake.
        if native != _GO_NATIVE_OF_KIND[kind]:
            if kind.endswith("_array"):
                element = native.removeprefix("[]")
                body.append("\tconverted%d := make(%s, len(%s))" % (index, native, name))
                body.append("\tfor i, item := range %s {" % name)
                body.append("\t\tconverted%d[i] = %s(item)" % (index, element))
                body.append("\t}")
                call_args.append("converted%d" % index)
            else:
                body.append("\tconverted%d := %s(%s)" % (index, native, name))
                call_args.append("converted%d" % index)
        else:
            call_args.append(name)

    for kind in used:
        lines.append(_GO_CONVERTERS[kind][1].strip("\n"))
        lines.append("")
    # frfInts and frfFloats call the scalar converters, so those have to be present even when no
    # scalar argument asked for them.
    for kind, dependency in (("int_array", "int"), ("float_array", "float")):
        if kind in used and dependency not in used:
            lines.append(_GO_CONVERTERS[dependency][1].strip("\n"))
            lines.append("")

    lines.append("// Entry is what serve.go calls. See frf/observe/call/bridge.py.")
    lines.append("func Entry(args []interface{}) (interface{}, error) {")
    lines.append("\tif len(args) != %d {" % len(params))
    lines.append('\t\treturn nil, fmt.Errorf("expected %d argument(s), got %%d", len(args))'
                 % len(params))
    lines.append("\t}")
    lines.extend(body)
    if result:
        lines.append("\treturn %s(%s), nil" % (symbol, ", ".join(call_args)))
    else:
        # A VOID FUNCTION IS OBSERVED THROUGH WHAT IT MUTATES. The miner only offers one when some
        # argument can carry a mutation, so the first array argument is called and then returned:
        # that slice IS the observable behaviour of an in-place sort, which is a third of this supply.
        carrier = next((n for n, param in enumerate(params)
                        if str(param.get("kind", "")).endswith("_array")), None)
        if carrier is None:
            raise Unsupported(
                "%s returns nothing and has no array argument to observe a mutation through" % symbol)
        lines.append("\t%s(%s)" % (symbol, ", ".join(call_args)))
        lines.append("\treturn %s, nil" % call_args[carrier])
    lines.append("}")
    return "\n".join(lines) + "\n"


def _reconcile_go(text: str) -> str:
    """A mined Go file, made able to compile beside `serve.go`. Two collisions, both measured.

    THE PACKAGE CLAUSE. Go requires every file in a directory to declare the same package, and the
    shim is `package main` while real material is `package sort`, `package algorithms`, whatever the
    repository chose. Unreconciled, the build fails with `found packages main (serve.go) and sort
    (subject.go)` -- which is what refused all four candidates of the first Go kernel batch, and says
    nothing about the material.

    A SECOND `main`. A repository that ships a runnable program has `func main()` in it, and the shim
    has one too: `main redeclared in this block`. The mined one is renamed rather than deleted --
    deleting spans is how a file gets truncated mid-expression, and an unused function is legal in Go
    where an unused import is not. Nothing calls a `main`, so renaming it changes no behaviour of the
    function under test.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("package ") and len(stripped.split()) == 2:
            # The FIRST package clause only; the word also appears in comments and strings.
            if not any(l.startswith("package main") for l in out):
                out.append("package main")
                continue
        if stripped.startswith("func main(") or stripped.startswith("func main "):
            out.append(line.replace("func main(", "func frfUnusedMain(", 1))
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


_GENERATORS = {"go": _go}

# How a mined file is made to compile beside its shim. Separate from `_GENERATORS` because a language
# can need one without the other: Rust reaches the subject as `mod subject`, so its file needs no
# package surgery at all, while Go cannot compile without it.
_RECONCILERS = {"go": _reconcile_go}


def reconcile(language: str, text: str) -> str:
    """The mined source, adjusted so it can be compiled beside the shim.

    Returned unchanged for a language that needs nothing, so a caller can apply this unconditionally
    rather than deciding per language -- which is the kind of decision that ends up in two places.
    """
    reconciler = _RECONCILERS.get((language or "").strip().lower())
    return reconciler(text) if reconciler else text


def supported(language: str) -> bool:
    """Whether a mined function in `language` can be bound to its shim."""
    return (language or "").strip().lower() in _GENERATORS


def source(language: str, *, symbol: str, params: list, result: dict | None = None,
           package: str = "") -> str:
    """The bridge source for one mined function.

    `params` are the schema entries the miner produced, each carrying `kind` and the source's own
    `native` spelling. `result` is the same shape for what the function returns, `{}` for void.
    `package` is what the mined file declared, which the caller may need to reconcile separately.
    """
    key = (language or "").strip().lower()
    generator = _GENERATORS.get(key)
    if generator is None:
        raise Unsupported(
            "no call bridge for %s: a static shim cannot bind a mined symbol without one, and "
            "generating source in the wrong language would fail the build as though the material "
            "were broken (supported today: %s)"
            % (language, ", ".join(sorted(_GENERATORS)) or "(none)"))
    if not symbol:
        raise Unsupported("a bridge needs the symbol it is binding")
    return generator(symbol, list(params or ()), dict(result or {}), package or "")
