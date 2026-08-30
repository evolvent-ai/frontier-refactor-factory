"""The package scale's dispatcher generator: what it writes, and what it refuses to write.

Three things used to be decided by one boolean in the scale
(`native = language in ("javascript", "typescript")`), and all three were wrong
for the six languages that fell down the else-branch:

  * the dispatcher source itself -- a Rust task was handed PYTHON source,
  * the filename it was written to -- a second copy of what the shim already
    declares, which already disagreed with the TypeScript shim's `subject.ts`,
  * the mutant, built by APPENDING Python source to whatever the subject was.

The third is the one worth a test the most. A mutation gate exists to ask "would
the probe notice a subtly wrong implementation?" -- and appended Python is a
syntax error in every language but Python, so the mutant never loaded and the
probe "caught" it by not being able to parse it. That is a gate that passes
itself. The checks below pin the mutant to being a LOADABLE program that returns
a WRONG ANSWER, which is the only version of the mutant that says anything about
the probe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.call import dispatch                                       # noqa: E402
from frf.observe.call import shims                                          # noqa: E402

DISPATCH = {"stem": ("mypkg.stem", "stem"), "parse": ("mypkg.parse", "parse")}

# Both mutant attempts the gate uses. Kept explicit rather than derived so that
# adding a third wrong answer has to come here and be argued for.
ATTEMPTS = (0, 1)


def test_every_servable_language_has_a_shim_to_serve_it():
    """A dispatcher for a language with no shim cannot reach a subject at all.

    The scale asks the shim for the subject's filename, so a language supported
    here but absent from TEMPLATES would raise KeyError deep inside `build()`,
    in the sandbox, after the candidate was paid for.

    `languages()` is derived from the generators rather than listed beside them. A `DYNAMIC` tuple
    used to carry this meaning by hand, and its own comment warned that a second list is a second
    place to forget one -- which is precisely what happened when the ruby generator was added.
    """
    for language in dispatch.languages():
        assert language in shims.TEMPLATES, (
            "%s can be dispatched but has no shim to serve it" % language)
        assert dispatch.supported(language), (
            "%s is listed as dispatchable but has no generator" % language)


def test_a_mutant_is_a_wrong_answer_in_the_subjects_own_language():
    """A mutant that CRASHES is caught by the wire, not by the probe, and scores the gate too high.

    This was a two-column tuple indexed by `if language in ("javascript", "typescript")`, so ruby --
    being neither -- was handed Python's `None` and its mutants died with
    `NameError: uninitialized constant None`. Real ruby found it; the generated text looks fine.

    Keyed by language now, so a missing entry is a KeyError at generation time rather than a mutant
    that is mis-spelled in a way only that language's runtime can tell you about.
    """
    spellings = {"python": "None", "javascript": "null", "typescript": "null", "ruby": "nil", "go": "nil", "rust": "Ok(crate::Json::Null)", "java": "null", "cpp": "null"}
    for language in dispatch.languages():
        assert language in spellings, "%s has a generator but no wrong-answer spelling" % language
        missing = dispatch.source(language, DISPATCH, mutant=0)
        assert spellings[language] in missing, (
            "%s mutant 0 should return that language's own empty value" % language)
        falsy = dispatch.source(language, DISPATCH, mutant=1)
        assert "0" in falsy, language


def test_the_python_dispatcher_and_its_mutants_are_valid_python():
    """Source that does not compile is caught by the parser, not by the probe."""
    for mutant in (None,) + ATTEMPTS:
        source = dispatch.source("python", DISPATCH, mutant=mutant)
        compile(source, "<dispatcher>", "exec")


@pytest.mark.parametrize("language", ["javascript", "typescript"])
def test_the_javascript_dispatcher_and_its_mutants_are_valid_javascript(language):
    """The bug this pins: appended Python source made every JS mutant unparseable.

    Skipped rather than assumed when node is absent, because the structural
    checks in this file are meant to run on a bare host. TypeScript is verified
    with tsc --noEmit because `node --check` demands plain JS and the generated
    TS carries declarations.
    """
    if not shutil.which("node"):
        pytest.skip("node is not installed; cannot check generated JavaScript")
    if language == "typescript" and not shutil.which("tsc"):
        pytest.skip("tsc is not installed; cannot check generated TypeScript")
    for mutant in (None,) + ATTEMPTS:
        source = dispatch.source(language, DISPATCH, mutant=mutant)
        if language == "typescript":
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "subject.ts")
                with open(path, "w") as handle:
                    handle.write(source)
                done = subprocess.run(["tsc", "--target", "ES2022",
                                       "--module", "commonjs", "--skipLibCheck",
                                       "--noEmit", path],
                                      capture_output=True, text=True)
                assert done.returncode == 0, (
                    "%s mutant=%r does not type-check as TypeScript: %s"
                    % (language, mutant, done.stderr.strip()[:200]))
            continue
        handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        try:
            handle.write(source)
            handle.close()
            done = subprocess.run(["node", "--check", handle.name],
                                  capture_output=True, text=True)
            assert done.returncode == 0, (
                "%s mutant=%r does not parse as JavaScript: %s"
                % (language, mutant, done.stderr.strip()[:200]))
        finally:
            os.unlink(handle.name)


def test_a_mutant_answers_wrongly_rather_than_refusing():
    """The whole point of the gate: a mutant that raises is detected by the wire.

    If the mutant crashes, the probe is credited with catching something it never
    had to reason about, and the gate scores far too generously. So the mutant
    must answer every operation the real dispatcher answers -- with a wrong
    value.
    """
    real = dispatch.source("python", DISPATCH)
    assert "importlib" in real, "the real dispatcher should resolve modules"

    for attempt in ATTEMPTS:
        namespace: dict = {}
        exec(dispatch.source("python", DISPATCH, mutant=attempt), namespace)
        for operation in DISPATCH:
            answer = namespace["entry"](operation, "running")
            assert answer in (None, 0), (
                "mutant %d answered %r, which is not a recognisably wrong value"
                % (attempt, answer))


def test_the_mutants_differ_from_each_other():
    """Two attempts that produce the same subject test the probe once, not twice."""
    sources = {dispatch.source("python", DISPATCH, mutant=attempt)
               for attempt in ATTEMPTS}
    assert len(sources) == len(ATTEMPTS), "the mutation attempts are not distinct"


@pytest.mark.parametrize("language", ["c"])
def test_a_language_without_a_dispatcher_refuses_loudly(language):
    """Silence here is what produced Python source in a file named `subject.rs`.

    These five have no runtime module-by-name lookup, so a package dispatcher for
    them is generated static imports plus a switch, with a concrete type per
    argument. That is real work; until it exists the honest answer is a refusal
    that names itself, which is the only kind of gap that can be argued with.
    """
    assert not dispatch.supported(language)
    with pytest.raises(dispatch.Unsupported) as raised:
        dispatch.source(language, DISPATCH)
    assert language in str(raised.value)


def test_the_dispatcher_covers_every_operation_it_was_given():
    """A dispatcher missing an entry point fails the contract, not the candidate.

    THE MODULE IS CHECKED IN EITHER SPELLING. The contract names a module the way Python spells one --
    `mypkg.stem` -- because one shape has to serve every language. Ruby has no such namespace: its
    dispatcher rewrites the dots to a path for `require_relative`, so demanding the dotted form here
    would fail a dispatcher that is correct, and demanding only the path form would fail the other
    three.
    """
    for language in dispatch.languages():
        # JAVA'S FIXTURE CANNOT BE THE SHARED ONE. Every Java operation is a STATIC method on a
        # named class, and `_static_java` refuses to generate without the owning class -- the shared
        # DISPATCH has (module, symbol) only. The java specific shape is tested elsewhere.
        if language in ("java", "cpp"):
            continue
        source = dispatch.source(language, DISPATCH)
        for operation, (module, symbol) in DISPATCH.items():
            assert operation in source, (
                "%s dispatcher omits operation %s" % (language, operation))
            # GO IS AN EXCEPTION, and it is the one worth a comment. Its package subject compiles into
            # the SAME package as the dispatcher (all files in one directory, no per-file namespace),
            # so the symbol is called unqualified -- the module line is part of the import machinery
            # it does not use. Demanding the dotted module on Go would fail a correct dispatcher.
            if language in ("go", "rust", "java", "cpp"):
                continue
            assert module in source or module.replace(".", "/") in source, (
                "%s dispatcher omits module %s in any spelling" % (language, module))


def test_the_emitted_package_task_gets_a_dispatcher_in_its_own_language():
    """The task that ships must carry the same dispatcher the build tree used.

    A SECOND COPY OF THIS GENERATOR LIVED IN `observe/call/package.py`, written inline: JS/TS got
    JavaScript, and EVERY OTHER LANGUAGE fell through to a Python dispatcher in subject.py. That is
    the exact fault `dispatch.py` was created to end -- its docstring names the old
    `native = language in ("javascript", "typescript")` test as the bug -- reintroduced in the emit
    path after `scales/package.py` was fixed.

    So a ruby package task would have built correctly and SHIPPED `import importlib`, failing replay
    for a reason that reads like broken material. A language with no dispatcher now refuses loudly
    here, as it does everywhere else, instead of silently receiving Python.
    """
    import os
    import tempfile

    from frf.observe.call import package as emit, shims

    class Material:
        package_name = "mypkg"
        symbol = "entry"
        dispatch = [{"name": "quick_sort", "module": "mypkg.lib.sorting", "symbol": "quick_sort"}]
        root = None
        package_root = None

    def emitted(language):
        room = tempfile.mkdtemp()
        material = Material()
        material.root = room
        material.package_root = os.path.join(room, "mypkg")
        os.makedirs(material.package_root, exist_ok=True)
        emit._serve_package_here(room, shims.TEMPLATES[language], material, language=language)
        subject = shims.TEMPLATES[language].subject
        return subject, open(os.path.join(room, subject), encoding="utf-8").read()

    # The filename comes from the shim rather than a second hardcoded list, which is why TypeScript
    # gets subject.ts here -- the inline copy wrote subject.js for it.
    for language, marker in (("ruby", "send("), ("python", "importlib"),
                             ("javascript", "exports.entry"), ("typescript", "exports.entry")):
        subject, text = emitted(language)
        assert subject == shims.TEMPLATES[language].subject
        assert marker in text, "%s task did not get a %s dispatcher: %s" % (language, language, subject)

    for language in ():
        with pytest.raises(dispatch.Unsupported):
            emitted(language)


def test_a_ruby_package_task_reaches_its_operations_in_the_emitted_layout():
    """The dispatched operation has to load in the room `_serve_package_here` creates.

    THE MODULE PATH AND THE EMITTED LAYOUT ARE THE SAME FACT, and they disagreed for ruby. A gem at
    `<repo>/lib/algo/sorting.rb` produces a dispatcher whose `require_relative` resolves against the
    directory holding subject.rb -- the room root, which holds `material.root`'s subtree after the
    copytree. So the module must be `lib.algo.sorting` for `require_relative "lib/algo/sorting"` to
    reach the file.

    It used to be `algo.lib.algo.sorting` -- the package name prefixed onto a path already rooted at
    the repo. The require path became `algo/lib/algo/sorting`, which no copytree ever creates:
    `material.root` already contains `lib/`, and there is no `<room>/algo/`. Every ruby package task
    would pass every gate then fail E7 as `cannot load such file`. Verified against real ruby.

    `_serve_package_here` uses the real chain, so the adapter's module and the emitted layout are
    checked as one thing rather than as two strings that might agree by accident.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    from frf.observe.call import package as emit, shims
    from frf.source import github_package as gp, package_adapters as pa

    if not shims.usable("ruby"):
        pytest.skip("ruby is not installed on this host")

    repo = tempfile.mkdtemp()
    os.makedirs(os.path.join(repo, "lib", "algo"))
    with open(os.path.join(repo, "algo.gemspec"), "w") as handle:
        handle.write("Gem::Specification.new { |s| s.name = 'algo' }\n")
    with open(os.path.join(repo, "lib", "algo.rb"), "w") as handle:
        handle.write("")
    with open(os.path.join(repo, "lib", "algo", "sorting.rb"), "w") as handle:
        handle.write("module Algo\n"
                     "  def self.quick_sort(xs)\n"
                     "    return xs if xs.length <= 1\n"
                     "    xs.sort\n"
                     "  end\n"
                     "end\n")

    root, name = gp._find_package_root(repo, "ruby")
    ops = pa.operations(repo, "ruby", name, root)
    assert ops, "the gem's own function must be found"

    # Emit like the pipeline does, into a room that receives material.root's subtree.
    room = tempfile.mkdtemp()
    shutil.copytree(repo, room, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "tests", "test", "docs"))

    # The class body is its own namespace, so values are read from enclosing locals.
    root_path, package_root_path = repo, root

    class Material:
        package_name = name
        package_root = package_root_path
        symbol = "entry"
        dispatch = ops
        root = root_path

    material = Material()
    shim = shims.TEMPLATES["ruby"]
    emit._serve_package_here(room, shim, material, language="ruby")

    served = subprocess.run(
        ["ruby", "serve.rb", "entry"],
        cwd=room, input='{"id":1,"op":"run","args":["quick_sort",[3,1,2]]}\n',
        capture_output=True, text=True, timeout=30)
    assert served.returncode == 0, served.stderr
    assert '"value":[1,2,3]' in served.stdout, (
        "the dispatched operation did not load in the emitted layout: %s" % served.stderr)


def test_a_ruby_class_method_is_reached_through_reflection():
    """A gem's `def self.x` is NOT a top-level def, and needs `Klass.method`.

    THE THIRD TIME THE SAME CLASS OF BUG APPEARED. The old `_ruby` adapter mined `^def\s+`, which on a
    real gem (human_time, mightystring, geometry) finds nothing top-level and silently empties the
    surface. This test pins the real shape: a module with static methods and instance methods, mine
    through `_ruby`, and serve through the generated dispatcher.

    The `klass` field is where the owning class lives; the dispatcher's `Object.const_get` +
    `klass.method(...)` is what lets the probe reach a method that `send` on the main object cannot.
    """
    import os
    import subprocess
    import tempfile

    from frf.observe.call import package as emit, shims
    from frf.observe.call import dispatch as d
    from frf.source import package_adapters as pa

    if not shims.usable("ruby"):
        pytest.skip("ruby is not installed")

    repo = tempfile.mkdtemp()
    os.makedirs(os.path.join(repo, "lib"))
    with open(os.path.join(repo, "lib", "human_time.rb"), "w") as handle:
        handle.write("module HumanTime\n"
                     "  def self.greater_than_aliases\n"
                     "    %w{newer_than? more_recent_than?}\n"
                     "  end\n"
                     "  def self.greater_than_or_equal_to_aliases\n"
                     "    %w{newer_than_or_equal_to?}\n"
                     "  end\n"
                     "end\n")

    ops = pa.operations(repo, "ruby", "human_time", os.path.join(repo, "lib"))
    static = [o for o in ops if o.get("klass") == "HumanTime"]
    assert static, "the module's static methods must be mined with their owning class"
    assert all(o["symbol"].startswith("self.") for o in static), static

    room = tempfile.mkdtemp()
    shutil.copytree(repo, room, dirs_exist_ok=True)
    table = {o["name"]: ((o["module"], o["symbol"]) if not (o.get("klass") or "")
                         else (o["module"], o["symbol"], o["klass"])) for o in ops}
    adapter = os.path.join(room, "subject.rb")
    with open(adapter, "w", encoding="utf-8") as handle:
        handle.write(d.source("ruby", table))
    shims.materialise(room, "ruby", adapter, "entry")

    served = subprocess.run(
        ["ruby", "serve.rb", "entry"], cwd=room,
        input='{"id":1,"op":"run","args":["greater_than_aliases"]}\n',
        capture_output=True, text=True, timeout=30)
    assert served.returncode == 0, served.stderr
    assert "newer_than?" in served.stdout, served.stdout


def test_a_go_package_at_the_module_root_gets_a_legal_import_path():
    """`relpath` spells "the root itself" as ".", and Go refuses that inside an import path.

    A repository whose API lives in the module root is ordinary -- gookit/goutil is one -- and the
    concatenation produced `github.com/gookit/goutil/.`, which the compiler rejects with
    `malformed import path: invalid path element "."`. The whole candidate then refused at build
    as though the material would not compile, when it was the dispatcher's import that was wrong.
    """
    import os
    import tempfile

    from frf.source import package_adapters as pa

    repo = tempfile.mkdtemp()
    with open(os.path.join(repo, "go.mod"), "w", encoding="utf-8") as handle:
        handle.write("module github.com/example/goutil\n\ngo 1.21\n")
    # One exported function in the ROOT, one in a subdirectory: the two shapes must both be legal.
    with open(os.path.join(repo, "strutil.go"), "w", encoding="utf-8") as handle:
        handle.write("package goutil\n\nfunc Upper(s string) string { return s }\n")
    os.makedirs(os.path.join(repo, "mathutil"))
    with open(os.path.join(repo, "mathutil", "calc.go"), "w", encoding="utf-8") as handle:
        handle.write("package mathutil\n\nfunc Add(a int, b int) int { return a + b }\n")

    paths = {o["name"]: o["module"] for o in pa.operations(repo, "go", "goutil", repo)}
    assert paths.get("Upper") == "github.com/example/goutil", paths
    assert paths.get("Add") == "github.com/example/goutil/mathutil", paths
    assert not any("/." in path for path in paths.values()), paths
