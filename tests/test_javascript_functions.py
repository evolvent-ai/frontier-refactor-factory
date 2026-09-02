from frf.source.javascript_functions import scan


def test_typescript_scan_accepts_explicit_json_safe_top_level_functions(tmp_path):
    path = tmp_path / "subject.ts"
    path.write_text("""
export function scale(values: number[], factor: number): number[] {
  return values.map((value) => value * factor);
}
function hidden(value: any): any { return value; }
function constant(value: number): number { return 1; }
""", encoding="utf-8")
    found = scan(str(tmp_path), "pkg", "1")
    assert [(item.module, item.symbol) for item in found] == [("subject", "scale")]
    assert found[0].schema["params"][0]["kind"] == "float_array"


def test_scan_excludes_nested_helpers_and_constant_scalar_workloads(tmp_path):
    (tmp_path / "subject.js").write_text("""
export function outer(values) {
  function nested(value) { return value; }
  return values.map((value) => value + 1);
}
/** @param {number} value */
export function constant(value) { return 1; }
""", encoding="utf-8")
    assert [item.symbol for item in scan(str(tmp_path), "pkg", "1")] == []


def test_scan_excludes_unexported_helpers_that_the_shim_cannot_dispatch(tmp_path):
    (tmp_path / "subject.js").write_text("""
function helper(value) { return value.map((item) => item + 1); }
/** @param {number[]} values */
exports.publicFn = (values) => values.map((item) => item + 1);
""", encoding="utf-8")
    found = scan(str(tmp_path), "pkg", "1")
    assert [item.symbol for item in found] == ["publicFn"]


def test_scan_accepts_commonjs_object_export_keys(tmp_path):
    (tmp_path / "subject.js").write_text("""
function decode(values) { return values.map((item) => item + 1); }
module.exports = { decode };
""", encoding="utf-8")
    # The adapter still requires an explicit JSDoc contract for JavaScript parameters.
    (tmp_path / "subject.js").write_text("/** @param {number[]} values */\n" +
        (tmp_path / "subject.js").read_text(), encoding="utf-8")
    assert [item.symbol for item in scan(str(tmp_path), "pkg", "1")] == ["decode"]


def test_scan_rejects_top_level_output_that_would_corrupt_jsonl(tmp_path):
    (tmp_path / "subject.ts").write_text(
        "export function distance(a: string, b: string): number { return a.length + b.length; }\n"
        "console.log(distance('a', 'b'));\n", encoding="utf-8")
    assert scan(str(tmp_path), "pkg", "1") == []


def test_javascript_scan_requires_jsdoc_for_untyped_parameters(tmp_path):
    path = tmp_path / "subject.js"
    path.write_text("""
/** @param {string} value */
export function reverse(value) { return value.split('').reverse().join(''); }
function unknown(value) { return value; }
""", encoding="utf-8")
    found = scan(str(tmp_path), "pkg", "1")
    assert [item.symbol for item in found] == ["reverse"]


def test_javascript_scan_rejects_importing_files_for_standalone_serving(tmp_path):
    (tmp_path / "subject.js").write_text("import x from 'x';\nexport function run(value) { return value; }\n",
                                          encoding="utf-8")
    assert scan(str(tmp_path), "pkg", "1") == []


def test_javascript_shim_dispatches_the_mined_symbol(tmp_path):
    from frf.observe.call import shims
    from frf.observe.call.runner import Subject

    (tmp_path / "source.js").write_text(
        "export const scale = (values, factor) => values.map((v) => v * factor);\n",
        encoding="utf-8")
    # The source scanner needs explicit JSDoc for JavaScript, but the shim itself only needs the
    # exported symbol and is tested independently here.
    _, argv = shims.materialise(str(tmp_path), "javascript", str(tmp_path / "source.js"), "scale")
    with Subject(argv, cwd=str(tmp_path), timeout=5) as subject:
        result = subject.call("scale", [[1, 2, 3], 2])
    assert result.ok and result.value == [2, 4, 6]


def test_typescript_shim_uses_compiler_output_instead_of_node_strip_flag():
    from frf.observe.call import shims
    shim = shims.load("typescript")
    assert shim.build and shim.build[0][0] == "tsc"
    assert "--experimental-strip-types" not in shim.run
    # COMPILED IN PLACE, not into a compiled/ subdirectory, so a package dispatcher's relative
    # imports resolve from the workspace root where the module tree lives. (The compiled/ form
    # moved that root and every package/ts candidate died in E3 with ERR_MODULE_NOT_FOUND.)
    assert "subject.js" in shim.run


def test_the_shim_is_commonjs_whatever_the_package_declares():
    """A package that declares `"type": "module"` makes every neighbouring `.js` file ESM.

    The shim is CommonJS, so it died with `ReferenceError: require is not defined` on eleven of
    thirteen javascript package candidates. `.cjs` is the escape hatch: CommonJS whatever the
    package says.

    The SUBJECT keeps `.js`, because it is not ours. A mined function is usually ESM
    (`export function ...`), and renaming it too makes Node parse it as CommonJS and fail with
    `SyntaxError: Unexpected token 'export'` -- which is exactly what happened when this was first
    written for both files at once.
    """
    from frf.observe.call.shims import TEMPLATES

    for language in ("javascript", "typescript"):
        shim = TEMPLATES[language]
        assert shim.template.endswith(".cjs"), \
            "%s's shim must be CommonJS regardless of the package: %s" % (language, shim.template)
        assert not shim.subject.endswith(".cjs"), \
            "%s's subject must keep its own module system: %s" % (language, shim.subject)
        assert not any(str(part).endswith(".cjs") for part in shim.run if part != shim.template), \
            "the subject is loaded by its own name: %r" % (shim.run,)
