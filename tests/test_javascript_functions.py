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
        "exports.scale = (values, factor) => values.map((v) => v * factor);\n",
        encoding="utf-8")
    # The source scanner needs explicit JSDoc for JavaScript, but the shim itself only needs the
    # exported symbol and is tested independently here.
    _, argv = shims.materialise(str(tmp_path), "javascript", str(tmp_path / "source.js"), "scale")
    with Subject(argv, cwd=str(tmp_path), timeout=5) as subject:
        result = subject.call("scale", [[1, 2, 3], 2])
    assert result.ok and result.value == [2, 4, 6]
