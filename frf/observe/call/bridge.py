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
    "bytes": ("frfBytes", """
func frfBytes(value interface{}) ([]byte, bool) {
	// JSON carries bytes as a base64 string; the wire's own encoder produced it that way.
	text, ok := value.(string)
	if !ok {
		return nil, false
	}
	decoded, err := base64.StdEncoding.DecodeString(text)
	return decoded, err == nil
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
    "int_array": "[]int", "float_array": "[]float64", "bytes": "[]byte",
}


def _go(symbol: str, params: list, result: dict, package: str, owner: str = "") -> str:
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


# ----------------------------------------------------------------------------------- Rust
#
# The bridge is APPENDED TO THE SUBJECT here, not written beside it: `rustc -o bin serve.rs` names
# only the shim, which reaches the subject as `mod subject`, so a third file would be invisible to the
# compiler. Being one module has a compensating benefit -- the mined function is in scope under its
# plain name, which is why `_reachable` refuses anything inside an `impl` or a `mod`.
_RUST_CONVERTERS = {
    "int": ("frf_i64", """
fn frf_i64(value: &crate::Json) -> Result<i64, String> {
    // JSON has one number type, so an integer arrives as f64. A non-integral value is REFUSED rather
    // than truncated: dropping .5 would have the subject answer a question it was not asked.
    match value.as_f64() {
        Some(number) if number.fract() == 0.0 => Ok(number as i64),
        _ => Err("expected an integer".to_string()),
    }
}
"""),
    "float": ("frf_f64", """
fn frf_f64(value: &crate::Json) -> Result<f64, String> {
    value.as_f64().ok_or_else(|| "expected a number".to_string())
}
"""),
    "bool": ("frf_bool", """
fn frf_bool(value: &crate::Json) -> Result<bool, String> {
    match value {
        crate::Json::Bool(flag) => Ok(*flag),
        _ => Err("expected a boolean".to_string()),
    }
}
"""),
    "string": ("frf_string", """
fn frf_string(value: &crate::Json) -> Result<String, String> {
    value.as_str().map(|text| text.to_string()).ok_or_else(|| "expected a string".to_string())
}
"""),
    "int_array": ("frf_i64s", """
fn frf_i64s(value: &crate::Json) -> Result<Vec<i64>, String> {
    let items = value.as_array().ok_or_else(|| "expected an array".to_string())?;
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        out.push(frf_i64(item)?);
    }
    Ok(out)
}
"""),
    "float_array": ("frf_f64s", """
fn frf_f64s(value: &crate::Json) -> Result<Vec<f64>, String> {
    let items = value.as_array().ok_or_else(|| "expected an array".to_string())?;
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        out.push(frf_f64(item)?);
    }
    Ok(out)
}
"""),
}

# The type each converter yields. A parameter spelled otherwise -- `i32`, `Vec<usize>` -- is cast from
# this, because the compiler will not do it silently and the mined spelling is what the call must match.
_RUST_OWNED = {"int": "i64", "float": "f64", "bool": "bool", "string": "String",
               "int_array": "Vec<i64>", "float_array": "Vec<f64>", "bytes": "Vec<u8>"}

# How a returned value becomes JSON. `{}` -- a void function -- is handled separately, through what it
# mutated, exactly as in Go.
_RUST_ENCODE = {
    "int": "crate::Json::Number(value as f64)",
    "float": "crate::Json::Number(value as f64)",
    "bool": "crate::Json::Bool(value)",
    "string": "crate::Json::String(value.to_string())",
    "int_array": "crate::Json::Array(value.iter().map(|item| "
                 "crate::Json::Number(*item as f64)).collect())",
    "float_array": "crate::Json::Array(value.iter().map(|item| "
                   "crate::Json::Number(*item as f64)).collect())",
}


def _rust_element(native: str) -> str:
    """The element type inside a Rust sequence spelling: `Vec<i32>` and `&[i32]` -> `i32`."""
    stripped = native.lstrip("&").replace("mut ", "").strip()
    if stripped.startswith("Vec<") and stripped.endswith(">"):
        return stripped[4:-1].strip()
    return stripped.strip("[]").strip()


def _rust(symbol: str, params: list, result: dict, package: str, owner: str = "") -> str:
    lines: list = []
    used: list = []
    body: list = []
    call_args: list = []
    void = not result

    for index, param in enumerate(params):
        kind = str(param.get("kind", ""))
        entry = _RUST_CONVERTERS.get(kind)
        if entry is None:
            raise Unsupported(
                "no Rust conversion for a %r argument; the bridge would not compile" % kind)
        converter = entry[0]
        if kind not in used:
            used.append(kind)
        native = " ".join(str(param.get("native", "")).split()) or _RUST_OWNED[kind]
        owned = _RUST_OWNED[kind]
        borrowed = native.startswith("&")
        # A void subject writes into its argument, so the binding has to be mutable for it to.
        mutable = "mut" if void and kind.endswith("_array") else ""
        name = "arg%d" % index
        body.append("    let %s%s = %s(&items[%d])?;"
                    % (mutable + " " if mutable else "", name, converter, index))
        # THE SPELLING MATTERS, NOT ONLY THE KIND. `Vec<i32>` and `Vec<i64>` are both `int_array`, and
        # the converter yields `Vec<i64>`; Rust will not coerce between them, so the cast is written.
        # `usize` is the common case in real material -- an index or a length.
        target_element = _rust_element(native)
        if kind.endswith("_array") and target_element != _rust_element(owned):
            body.append("    let %s%s: Vec<%s> = %s.into_iter().map(|item| item as %s).collect();"
                        % (mutable + " " if mutable else "", name, target_element, name,
                           target_element))
        elif not kind.endswith("_array") and native.lstrip("&").strip() != owned:
            body.append("    let %s = %s as %s;" % (name, name, native.lstrip("&").strip()))
        # A borrowed parameter takes a reference to what was just built. `&String` coerces to `&str`
        # and `&Vec<T>` to `&[T]`, so one rule covers both spellings.
        prefix = "&mut " if (borrowed and mutable) else ("&" if borrowed else "")
        call_args.append("%s%s" % (prefix, name))

    for kind in used:
        lines.append(_RUST_CONVERTERS[kind][1].strip("\n"))
        lines.append("")
    for kind, dependency in (("int_array", "int"), ("float_array", "float")):
        if kind in used and dependency not in used:
            lines.append(_RUST_CONVERTERS[dependency][1].strip("\n"))
            lines.append("")

    lines.append("// entry is what serve.rs calls. See frf/observe/call/bridge.py.")
    lines.append("pub fn entry(args: &crate::Json) -> Result<crate::Json, String> {")
    lines.append('    let items = args.as_array()'
                 '.ok_or_else(|| "the arguments must be a JSON array".to_string())?;')
    lines.append("    if items.len() != %d {" % len(params))
    lines.append('        return Err(format!("expected %d argument(s), got {}", items.len()));'
                 % len(params))
    lines.append("    }")
    lines.extend(body)
    if void:
        carrier = next((n for n, param in enumerate(params)
                        if str(param.get("kind", "")).endswith("_array")), None)
        if carrier is None:
            raise Unsupported(
                "%s returns nothing and has no array argument to observe a mutation through" % symbol)
        lines.append("    %s(%s);" % (symbol, ", ".join(call_args)))
        lines.append("    let value = arg%d;" % carrier)
        lines.append("    Ok(%s)" % _RUST_ENCODE[str(params[carrier]["kind"])])
    else:
        kind = str(result.get("kind", ""))
        if kind not in _RUST_ENCODE:
            raise Unsupported("no Rust encoding for a %r result" % kind)
        lines.append("    let value = %s(%s);" % (symbol, ", ".join(call_args)))
        lines.append("    Ok(%s)" % _RUST_ENCODE[kind])
    lines.append("}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------- Java
#
# APPENDED TO THE SUBJECT, like Rust but for a different reason: Serve.java reflects for
# `Class.forName("Subject")`, so the generated class has to BE `Subject` -- and a Java file may hold
# several top-level classes as long as the public one matches the filename. So `Subject.java` ends up
# holding the mined `class M` (stripped of `public`, see `_reconcile_java`) and `public class Subject`
# beside it.
#
# EVERY NAME IS FULLY QUALIFIED in what follows. Java requires imports above the first class
# declaration and the bridge is appended BELOW one, so there is nowhere to put an import.
_JAVA_CONVERTERS = {
    "int": ("frfInt", """
    static long frfInt(Object value) {
        // The shim's parser yields Long for an integral token and Double otherwise, so both arrive
        // here. A non-integral value is REFUSED rather than truncated.
        if (!(value instanceof Number)) throw new IllegalArgumentException("expected an integer");
        double number = ((Number) value).doubleValue();
        if (number != Math.rint(number)) throw new IllegalArgumentException("expected an integer");
        return (long) number;
    }
"""),
    "float": ("frfFloat", """
    static double frfFloat(Object value) {
        if (!(value instanceof Number)) throw new IllegalArgumentException("expected a number");
        return ((Number) value).doubleValue();
    }
"""),
    "bool": ("frfBool", """
    static boolean frfBool(Object value) {
        if (!(value instanceof Boolean)) throw new IllegalArgumentException("expected a boolean");
        return ((Boolean) value).booleanValue();
    }
"""),
    "string": ("frfString", """
    static String frfString(Object value) {
        if (!(value instanceof String)) throw new IllegalArgumentException("expected a string");
        return (String) value;
    }
"""),
    "int_array": ("frfInts", """
    static long[] frfInts(Object value) {
        if (!(value instanceof java.util.List)) throw new IllegalArgumentException("expected an array");
        java.util.List<?> items = (java.util.List<?>) value;
        long[] out = new long[items.size()];
        for (int i = 0; i < out.length; i++) out[i] = frfInt(items.get(i));
        return out;
    }
"""),
    "float_array": ("frfFloats", """
    static double[] frfFloats(Object value) {
        if (!(value instanceof java.util.List)) throw new IllegalArgumentException("expected an array");
        java.util.List<?> items = (java.util.List<?>) value;
        double[] out = new double[items.size()];
        for (int i = 0; i < out.length; i++) out[i] = frfFloat(items.get(i));
        return out;
    }
"""),
}

_JAVA_OWNED = {"int": "long", "float": "double", "bool": "boolean", "string": "String",
               "int_array": "long[]", "float_array": "double[]"}


def _java_element(native: str) -> str:
    return native.replace("[]", "").strip()


def _java_box_helper(native: str, kind: str) -> str:
    """A boxing helper for the EXACT array type being returned.

    A Java array is not a List and Serve.java's writer does not walk one, so an array result has to be
    boxed element by element -- handed back raw it would serialise as an opaque object and every
    expectation would be frozen against that rather than against the numbers.

    GENERATED FOR THE MINED SPELLING, because Java does not widen array types. Fixed `long[]` and
    `double[]` overloads looked reasonable and real javac refused them:

        error: no suitable method found for frfBox(int[])
          method Subject.frfBox(long[]) is not applicable
            (argument mismatch; int[] cannot be converted to long[])

    An `int[]` parameter is the common case in mined material, so that was most of the supply. The
    ELEMENT still widens on its way into the box -- `Long.valueOf(int)` is a widening conversion
    followed by boxing -- which is why one helper per array type is enough.
    """
    element = _java_element(native)
    boxed = "Long" if kind == "int_array" else "Double"
    return ("    static java.util.List<Object> frfBox(%s[] items) {\n"
            "        java.util.List<Object> out = new java.util.ArrayList<Object>();\n"
            "        for (%s item : items) out.add(%s.valueOf(item));\n"
            "        return out;\n"
            "    }\n" % (element, element, boxed))


def _java(symbol: str, params: list, result: dict, package: str, owner: str = "") -> str:
    if not owner:
        raise Unsupported(
            "a Java bridge needs the class holding %s: the generated Subject calls "
            "`Owner.method(...)` and cannot construct an instance" % symbol)
    used: list = []
    body: list = []
    call_args: list = []
    void = not result

    for index, param in enumerate(params):
        kind = str(param.get("kind", ""))
        entry = _JAVA_CONVERTERS.get(kind)
        if entry is None:
            raise Unsupported(
                "no Java conversion for a %r argument; the bridge would not compile" % kind)
        if kind not in used:
            used.append(kind)
        native = " ".join(str(param.get("native", "")).split()) or _JAVA_OWNED[kind]
        owned = _JAVA_OWNED[kind]
        name = "arg%d" % index
        body.append("        %s %s = %s(args.get(%d));" % (owned, name, entry[0], index))
        # THE SPELLING MATTERS. `int[]` and `long[]` are both `int_array` and the converter yields
        # `long[]`; Java narrows only with an explicit cast, and a mined `int[]` parameter is common.
        if native != owned:
            if kind.endswith("_array"):
                element = _java_element(native)
                body.append("        %s[] %sc = new %s[%s.length];" % (element, name, element, name))
                body.append("        for (int i = 0; i < %s.length; i++) %sc[i] = (%s) %s[i];"
                            % (name, name, element, name))
                call_args.append("%sc" % name)
            else:
                body.append("        %s %sc = (%s) %s;" % (native, name, native, name))
                call_args.append("%sc" % name)
        else:
            call_args.append(name)

    # THE CALL IS COMPOSED BEFORE THE CLASS IS ASSEMBLED, because a boxing helper has to be emitted
    # ABOVE `entry` and which one is needed depends on the exact array type being handed back.
    tail: list = []
    boxes: dict = {}
    if void:
        carrier = next((n for n, param in enumerate(params)
                        if str(param.get("kind", "")).endswith("_array")), None)
        if carrier is None:
            raise Unsupported(
                "%s returns nothing and has no array argument to observe a mutation through" % symbol)
        carried = str(params[carrier]["kind"])
        boxes[" ".join(str(params[carrier].get("native", "")).split())
              or _JAVA_OWNED[carried]] = carried
        tail.append("        %s.%s(%s);" % (owner, symbol, ", ".join(call_args)))
        tail.append("        return frfBox(%s);" % call_args[carrier])
    else:
        kind = str(result.get("kind", ""))
        if kind not in _JAVA_OWNED:
            raise Unsupported("no Java encoding for a %r result" % kind)
        spelling = " ".join(str(result.get("native", "")).split()) or _JAVA_OWNED[kind]
        if kind.endswith("_array"):
            # DECLARED WITH THE MINED SPELLING. `long[] value = M.f()` does not compile when f returns
            # `int[]`: Java widens an element, never an array.
            boxes[spelling] = kind
            tail.append("        %s value = %s.%s(%s);"
                        % (spelling, owner, symbol, ", ".join(call_args)))
            tail.append("        return frfBox(value);")
        else:
            # A scalar is declared as the converter's own type -- an `int` result widens into a `long`
            # -- and autoboxes on the way out through Object.
            tail.append("        %s value = %s.%s(%s);"
                        % (_JAVA_OWNED[kind], owner, symbol, ", ".join(call_args)))
            tail.append("        return value;")

    lines = ["public class Subject {"]
    for kind in used:
        lines.append(_JAVA_CONVERTERS[kind][1].strip("\n"))
    for kind, dependency in (("int_array", "int"), ("float_array", "float")):
        if kind in used and dependency not in used:
            lines.append(_JAVA_CONVERTERS[dependency][1].strip("\n"))
    for spelling, kind in boxes.items():
        lines.append(_java_box_helper(spelling, kind).rstrip("\n"))
    lines.append("")
    lines.append("    // entry is what Serve.java reflects for. See frf/observe/call/bridge.py.")
    lines.append("    public static Object entry(java.util.List<Object> args) {")
    lines.append("        if (args.size() != %d) {" % len(params))
    lines.append('            throw new IllegalArgumentException('
                 '"expected %d argument(s), got " + args.size());' % len(params))
    lines.append("        }")
    lines.extend(body)
    lines.extend(tail)
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _reconcile_java(text: str) -> str:
    """A mined Java file, made able to hold `public class Subject` beside its own class.

    ONE PUBLIC CLASS PER FILE, and its name must match the filename. The bridge is appended to
    `Subject.java` because Serve.java reflects for `Class.forName("Subject")`, so a mined
    `public class M` in that file is a compile error -- `class M is public, should be declared in a
    file named M.java`. Java is perfectly happy with several package-private top-level classes in one
    file, so dropping the modifier is all that is needed, and it changes nothing about the method the
    bridge calls: a static method stays reachable as `M.method(...)` from a class in the same file.

    A PACKAGE DECLARATION IS DROPPED for the same reason. It would put the mined class in a package
    while Serve.java looks for `Subject` in the default one, so the reflection would fail at run time
    with ClassNotFoundException -- after a successful build, which is the worst place to find out.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("package ") and stripped.endswith(";"):
            continue
        # `public class`, `public final class`, `public abstract class`, and the same for interfaces
        # and enums, which a mined file can also declare beside its class.
        if stripped.startswith("public ") and not line.startswith((" ", "\t")):
            words = stripped.split()
            if any(word in ("class", "interface", "enum", "record") for word in words[:4]):
                out.append(line.replace("public ", "", 1))
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# ----------------------------------------------------------------------------------- C++
#
# THE HEAVIEST OF THE FOUR, for one reason: serve.c is compiled AS C (`-x c`) and its JSON parser is
# `static`, so the bridge cannot reach it and has to read the argument array itself. The contract is
# also the rawest -- `extern "C" const char *entry(const char *args_json)` returning a malloc'd
# document that serve.c frees, refusing by returning NULL with a message left in `entry_error`.
#
# APPENDED TO subject.cpp, like Rust and Java: the mined function is then already declared above, so
# no forward declaration has to be synthesised from a type spelling.
_CPP_PREAMBLE = """
#include <cstdlib>
#include <cstring>
#include <exception>
#include <string>
#include <vector>

extern "C" { char *entry_error = nullptr; }

namespace frf {

// A JSON value, only as far as this wire carries one. Deliberately not a general parser: it reads the
// argument array serve.c hands over, and nothing else ever reaches it.
struct Value {
    enum Kind { Null, Bool, Number, String, Array } kind = Null;
    bool boolean = false;
    double number = 0.0;
    std::string text;
    std::vector<Value> items;
};

inline void skip(const char *&at) {
    while (*at == ' ' || *at == '\\t' || *at == '\\n' || *at == '\\r') at++;
}

bool parse(const char *&at, Value &out);

inline bool parse_string(const char *&at, std::string &out) {
    if (*at != '"') return false;
    at++;
    while (*at && *at != '"') {
        if (*at == '\\\\') {
            at++;
            switch (*at) {
                case 'n': out.push_back('\\n'); break;
                case 't': out.push_back('\\t'); break;
                case 'r': out.push_back('\\r'); break;
                case 'b': out.push_back('\\b'); break;
                case 'f': out.push_back('\\f'); break;
                // A \\uXXXX escape is carried as the bytes that spell it rather than decoded: this
                // side only ever hands the string back to the subject, and a half-decoded surrogate
                // pair would be a different string from the one the factory sent.
                case 'u': out.append("\\\\u"); for (int i = 0; i < 4 && at[1]; i++) out.push_back(*++at); break;
                case '\\0': return false;
                default: out.push_back(*at);
            }
            at++;
            continue;
        }
        out.push_back(*at++);
    }
    if (*at != '"') return false;
    at++;
    return true;
}

inline bool parse(const char *&at, Value &out) {
    skip(at);
    if (*at == 'n' && std::strncmp(at, "null", 4) == 0) { at += 4; out.kind = Value::Null; return true; }
    if (*at == 't' && std::strncmp(at, "true", 4) == 0) {
        at += 4; out.kind = Value::Bool; out.boolean = true; return true;
    }
    if (*at == 'f' && std::strncmp(at, "false", 5) == 0) {
        at += 5; out.kind = Value::Bool; out.boolean = false; return true;
    }
    if (*at == '"') { out.kind = Value::String; return parse_string(at, out.text); }
    if (*at == '[') {
        at++;
        out.kind = Value::Array;
        skip(at);
        if (*at == ']') { at++; return true; }
        for (;;) {
            Value item;
            if (!parse(at, item)) return false;
            out.items.push_back(item);
            skip(at);
            if (*at == ',') { at++; continue; }
            if (*at == ']') { at++; return true; }
            return false;
        }
    }
    if (*at == '{') {
        // An object is not something any parameter of this wire is drawn as, so it is READ AND
        // REFUSED rather than skipped: silently accepting one would call the subject with a
        // default-constructed value and charge the answer to it.
        return false;
    }
    {
        char *end = nullptr;
        double number = std::strtod(at, &end);
        if (end == at) return false;
        at = end;
        out.kind = Value::Number;
        out.number = number;
        return true;
    }
}

// A malloc'd copy, because serve.c frees what entry returns.
inline const char *own(const std::string &text) {
    char *out = static_cast<char *>(std::malloc(text.size() + 1));
    if (out == nullptr) return nullptr;
    std::memcpy(out, text.c_str(), text.size() + 1);
    return out;
}

inline std::string number_text(double value) {
    // %.17g round-trips a double exactly, and an integral value is written without a fractional part
    // so that an integer answer is frozen as an integer.
    char buffer[40];
    if (value == static_cast<double>(static_cast<long long>(value))) {
        std::snprintf(buffer, sizeof buffer, "%lld", static_cast<long long>(value));
    } else {
        std::snprintf(buffer, sizeof buffer, "%.17g", value);
    }
    return std::string(buffer);
}

inline bool as_int(const Value &value, long long &out) {
    if (value.kind != Value::Number) return false;
    if (value.number != static_cast<double>(static_cast<long long>(value.number))) return false;
    out = static_cast<long long>(value.number);
    return true;
}

}  // namespace frf

// The message entry_error points at. Static storage: serve.c reads it and never frees it.
static std::string frf_message;

static const char *frf_refuse(const char *why) {
    frf_message = why;
    entry_error = const_cast<char *>(frf_message.c_str());
    return nullptr;
}
"""

_CPP_OWNED = {"int": "long long", "float": "double", "bool": "bool", "string": "std::string",
              "int_array": "std::vector<long long>", "float_array": "std::vector<double>"}


def _cpp_element(native: str) -> str:
    """The element type inside a C++ sequence spelling: `std::vector<int>` -> `int`."""
    stripped = native.replace("const", " ").replace("&", " ").strip()
    if "<" in stripped and stripped.endswith(">"):
        return stripped[stripped.index("<") + 1:-1].strip()
    return stripped


def _cpp_extract(index: int, kind: str, native: str, void: bool) -> list:
    """The lines that turn one JSON value into one typed C++ variable.

    EVERY GENERATED NAME IS PREFIXED, because a variable shadows a function in C++ and the mined
    symbol shares this scope. A subject whose function is called `at` -- an entirely ordinary name for
    element access -- met a local `const char *at` and the build failed with `'at' cannot be used as a
    function`, charged to the material. `items`, `value`, `out` and `parsed` are the same hazard.
    """
    name = "frf_arg%d" % index
    lines = []
    if kind == "int":
        lines.append("    long long %s = 0;" % name)
        lines.append("    if (!frf::as_int(frf_items[%d], %s)) "
                     'return frf_refuse("argument %d is not an integer");' % (index, name, index))
    elif kind == "float":
        lines.append("    if (frf_items[%d].kind != frf::Value::Number) "
                     'return frf_refuse("argument %d is not a number");' % (index, index))
        lines.append("    double %s = frf_items[%d].number;" % (name, index))
    elif kind == "bool":
        lines.append("    if (frf_items[%d].kind != frf::Value::Bool) "
                     'return frf_refuse("argument %d is not a boolean");' % (index, index))
        lines.append("    bool %s = frf_items[%d].boolean;" % (name, index))
    elif kind == "string":
        lines.append("    if (frf_items[%d].kind != frf::Value::String) "
                     'return frf_refuse("argument %d is not a string");' % (index, index))
        lines.append("    std::string %s = frf_items[%d].text;" % (name, index))
    elif kind in ("int_array", "float_array"):
        element = _cpp_element(native) or ("long long" if kind == "int_array" else "double")
        lines.append("    if (frf_items[%d].kind != frf::Value::Array) "
                     'return frf_refuse("argument %d is not an array");' % (index, index))
        lines.append("    std::vector<%s> %s;" % (element, name))
        lines.append("    for (const frf::Value &frf_item : frf_items[%d].items) {" % index)
        if kind == "int_array":
            lines.append("        long long frf_scalar = 0;")
            lines.append("        if (!frf::as_int(frf_item, frf_scalar)) "
                         'return frf_refuse("argument %d has a non-integer element");' % index)
            lines.append("        %s.push_back(static_cast<%s>(frf_scalar));" % (name, element))
        else:
            lines.append("        if (frf_item.kind != frf::Value::Number) "
                         'return frf_refuse("argument %d has a non-numeric element");' % index)
            lines.append("        %s.push_back(static_cast<%s>(frf_item.number));" % (name, element))
        lines.append("    }")
    else:
        raise Unsupported("no C++ conversion for a %r argument; the bridge would not compile" % kind)
    return lines


def _cpp_encode(kind: str, expression: str) -> list:
    """The lines that turn a returned value into the JSON document serve.c expects."""
    if kind in ("int", "float"):
        return ["    return frf::own(frf::number_text(static_cast<double>(%s)));" % expression]
    if kind == "bool":
        return ['    return frf::own(%s ? "true" : "false");' % expression]
    if kind == "string":
        return ["    std::string frf_out = \"\\\"\";",
                "    for (char frf_letter : %s) {" % expression,
                "        if (frf_letter == '\"' || frf_letter == '\\\\') frf_out.push_back('\\\\');",
                "        frf_out.push_back(frf_letter);",
                "    }",
                "    frf_out.push_back('\"');",
                "    return frf::own(frf_out);"]
    if kind in ("int_array", "float_array"):
        return ["    std::string frf_out = \"[\";",
                "    for (std::size_t frf_i = 0; frf_i < %s.size(); frf_i++) {" % expression,
                "        if (frf_i) frf_out.push_back(',');",
                "        frf_out += frf::number_text(static_cast<double>(%s[frf_i]));" % expression,
                "    }",
                "    frf_out.push_back(']');",
                "    return frf::own(frf_out);"]
    raise Unsupported("no C++ encoding for a %r result" % kind)


def _cpp(symbol: str, params: list, result: dict, package: str, owner: str = "") -> str:
    void = not result
    lines = [_CPP_PREAMBLE.strip("\n"), "",
             "// The work; `entry` below wraps it so a thrown exception cannot cross into serve.c.",
             "static const char *frf_call(const char *args_json) {",
             "    const char *frf_at = args_json;",
             "    frf::Value frf_parsed;",
             '    if (!frf::parse(frf_at, frf_parsed) || frf_parsed.kind != frf::Value::Array) '
             'return frf_refuse("the arguments must be a JSON array");',
             "    const std::vector<frf::Value> &frf_items = frf_parsed.items;",
             "    if (frf_items.size() != %d) "
             'return frf_refuse("wrong number of arguments");' % len(params)]
    call_args = []
    for index, param in enumerate(params):
        kind = str(param.get("kind", ""))
        native = " ".join(str(param.get("native", "")).split())
        lines.extend(_cpp_extract(index, kind, native, void))
        # A reference parameter is what a void subject writes through, and a `const &` is just a
        # borrow. Either way the variable is passed by name; C++ binds the reference itself.
        call_args.append("frf_arg%d" % index)
    if void:
        carrier = next((n for n, param in enumerate(params)
                        if str(param.get("kind", "")).endswith("_array")), None)
        if carrier is None:
            raise Unsupported(
                "%s returns nothing and has no array argument to observe a mutation through" % symbol)
        lines.append("    %s(%s);" % (symbol, ", ".join(call_args)))
        lines.extend(_cpp_encode(str(params[carrier]["kind"]), "frf_arg%d" % carrier))
    else:
        lines.append("    auto frf_value = %s(%s);" % (symbol, ", ".join(call_args)))
        lines.extend(_cpp_encode(str(result.get("kind", "")), "frf_value"))
    lines.append("}")
    lines.extend([
        "",
        "// entry is what serve.c calls. See frf/observe/call/bridge.py.",
        'extern "C" const char *entry(const char *args_json) {',
        "    // A THROWN EXCEPTION CANNOT CROSS THIS BOUNDARY, and serve.c has no way to catch one:",
        "    // it is compiled as C. An exception escaping here terminates the process, so every probe",
        "    // queued behind it is lost -- the same fault that cost the Rust shim 47% to 100% of a",
        "    // corpus before it caught unwinds. std::out_of_range from .at(), std::bad_alloc, a",
        "    // std::domain_error: all of them are ways real C++ refuses, and a refusal is an ANSWER.",
        "    try {",
        "        return frf_call(args_json);",
        "    } catch (const std::exception &failure) {",
        "        return frf_refuse(failure.what());",
        "    } catch (...) {",
        "        // A thrown int, a thrown string, a custom type: no message to report, but still an",
        "        // answer rather than a dead process.",
        '        return frf_refuse("the subject threw a non-standard exception");',
        "    }",
        "}",
    ])
    return "\n".join(lines) + "\n"


def _reconcile_cpp(text: str) -> str:
    """A mined C++ file with the standard headers hoisted above it.

    THE BRIDGE IS APPENDED, so its own `#include <vector>` lands BELOW the subject -- which is legal
    C++ and useless to a subject that needed the declaration earlier. Mined headers make this bite:
    a `.h` is not a translation unit, it is written to be included, and it relies on its includer for
    `<vector>` and `<string>`. Compiled standalone as subject.cpp it fails with

        error: 'vector' was not declared in this scope

    which is a fact about how we compiled it, not about the material. Hoisting the headers the bridge
    needs anyway also gives a mined header the declarations its includer would have provided. Include
    guards make the repetition free, and prepending cannot change the meaning of code that already
    included them.

    A SECOND `main`, exactly as Go has. `serve.c` defines one, and a mined `.cpp` from a repository
    that ships a runnable program defines another: `multiple definition of 'main'; serve.o:serve.c:
    first defined here`. Go has renamed its collision since the first kernel batch; C++ never did,
    and it refused a real candidate (huihut/interview's CountSort.cpp) the same way. Renamed rather
    than deleted, for Go's reason: cutting a span truncates a file mid-expression, and an uncalled
    function is legal. Nothing calls a `main` here, so the function under test is unaffected.
    """
    text = _rename_cpp_main(text)
    if _CPP_HOISTED in text:
        return text
    return "%s\n%s" % (_CPP_HOISTED, text)


def _rename_cpp_main(text: str) -> str:
    """`int main(` -> `int frfUnusedMain(`, so a mined program links beside the shim's own main.

    Line-based and conservative, like `_reconcile_go`. The name must be followed by its parameter
    list, so `mainLoop(` and `domain(` are untouched, and a leading return type is required so that
    a call to `main(...)` -- which real code does not contain -- is not rewritten either.
    """
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        for opener in ("int main(", "int main (", "void main(", "auto main("):
            if stripped.startswith(opener):
                head, _, rest = opener.partition("main")
                line = line.replace(opener, head + "frfUnusedMain" + rest, 1)
                break
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# Hoisted above a mined C++ file: the standard headers the bridge itself uses, and the `using` that a
# header's includer would have been carrying.
#
# WHY `using namespace std` AND NOT MORE HEADERS. A mined `.h` is written to be included, and the
# algorithm headers that make up this supply say `vector<int>` and `min(a, b)` unqualified because the
# `.cpp` that included them opened the namespace first. Compiled standalone they fail with
# `'min' was not declared in this scope; did you mean 'std::min'?` -- a fact about how we compiled the
# file, not about the material, which is precisely what reconciliation is for.
#
# It cannot hide a bad candidate. If the subject defines its own `min` at file scope, an unqualified
# call becomes AMBIGUOUS rather than silently resolving to std -- a compile error, loudly refused. And
# the generated bridge qualifies everything it touches (`std::string`, `std::vector`), so opening the
# namespace changes nothing it does. Adding headers beyond these would be guessing at what the subject
# needs; this is supplying what its own includer did.
_CPP_HOISTED = ("// --- frf: what a mined header's includer would have provided ---\n"
                "#include <cstdio>\n#include <cstdlib>\n#include <cstring>\n"
                "#include <string>\n#include <vector>\n#include <algorithm>\n"
                "using namespace std;")


_GENERATORS = {"go": _go, "rust": _rust, "java": _java, "cpp": _cpp}

# How a mined file is made to compile beside its shim. Separate from `_GENERATORS` because a language
# can need one without the other: Rust reaches the subject as `mod subject`, so its file needs no
# surgery at all, while Go, Java and C++ cannot compile without it.
_RECONCILERS = {"go": _reconcile_go, "java": _reconcile_java, "cpp": _reconcile_cpp}


# What marks generated text inside a mined file, so appending twice appends once. A comment rather
# than a sentinel file: it survives being copied, perturbed and read back, which is what the mutant
# path does to a subject.
MARKER = "// --- frf call bridge (generated) ---"


def attach(subject: str, generated: str) -> str:
    """A mined file with the bridge appended. Idempotent.

    FOR THE COMPILERS THAT ARE HANDED ONLY THE SHIM. rustc compiles `serve.rs`, which reaches the
    subject as `mod subject;`, so a separate bridge file is never seen -- the generated `entry` has to
    live in the subject's own module. That the two share a module is also what lets the bridge call the
    mined function by its plain name.

    IDEMPOTENT BECAUSE THE MUTANT PATH RE-MATERIALISES. E3 copies a workspace, perturbs the subject and
    materialises it again; appending a second `pub fn entry` would fail to compile as a duplicate
    definition, and E3 would score that as a detected mutation without the probe judging anything.
    """
    head = subject.split(MARKER, 1)[0].rstrip("\n")
    return "%s\n\n%s\n%s" % (head, MARKER, generated.strip("\n"))


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
           package: str = "", owner: str = "") -> str:
    """The bridge source for one mined function.

    `params` are the schema entries the miner produced, each carrying `kind` and the source's own
    `native` spelling. `result` is the same shape for what the function returns, `{}` for void.
    `package` is what the mined file declared, which the caller may need to reconcile separately.
    `owner` is the class holding the symbol, which only Java needs -- its bridge calls
    `Owner.method(...)` because every Java method lives in a class and cannot be reached without one.

    EVERY GENERATOR TAKES ALL FIVE, even the three that ignore `owner`. A signature that varied per
    language would put a branch here choosing which arguments to pass, which is one more place to
    forget a language when the next argument is added.
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
    # A SUBJECT THAT ALREADY OWNS THE ENTRY POINT'S NAME CANNOT BE BRIDGED. For the three languages
    # whose bridge is appended to the subject, `pub fn entry` beside a mined `entry` is a duplicate
    # definition; in Go the bridge is its own file but shares the package, so `Entry` collides just
    # the same. Refused here, where the reason can be stated, rather than as a compiler error about
    # a redefinition that reads like broken material.
    reserved = {"go": "Entry", "rust": "entry", "cpp": "entry", "java": "entry"}
    if symbol == reserved.get(key):
        raise Unsupported(
            "%s already defines %r, which is the name the %s shim's entry point must have"
            % (key, symbol, key))
    return generator(symbol, list(params or ()), dict(result or {}), package or "", owner or "")
