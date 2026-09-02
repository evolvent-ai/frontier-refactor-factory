"""Language-agnostic entry point discovery, and the danger of guessing.

The repo scale is meant to serve ANY language, and the mechanical manifest paths (pyproject,
Cargo.toml, pom.xml) cover only the languages they name. The language-agnostic paths -- CI
workflows, Justfile, Taskfile, devcontainer -- are what make a genuinely unknown language
servable, because every one of them is the maintainer saying how the project is built and run.

The danger is over-accepting. A CI file is mostly TESTING, and `cargo test` is not an entry
point: the corpus cannot freeze a test runner's output into a deterministic workload, and the
probes stage would refuse it -- after the checkout, E2B upload and build, the three most expensive
stages, have already been paid for. So the tests below check both halves: plausible entry
point commands are found, and commands that can only be a test/lint/build/network step are
refused up front rather than after the build.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.scales.repo import _ci_run_command, _justfile_recipe, _taskfile_command       # noqa: E402


def _with_ci(root: str, command: str) -> bool:
    """-> whether `_ci_run_command` accepts a workflow whose only step runs `command`."""
    workflows = os.path.join(root, ".github", "workflows")
    os.makedirs(workflows, exist_ok=True)
    with open(os.path.join(workflows, "ci.yml"), "w", encoding="utf-8") as handle:
        handle.write("name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                     "    steps:\n      - run: %s\n" % command)
    return _ci_run_command(root) is not None


def test_an_entry_point_command_is_found():
    """`cargo run`, `npm start`, `python main.py` are the shape of a program to freeze."""
    for command in ("cargo run --example demos", "npm run start", "go run ./cmd/x",
                    "python main.py", "make run"):
        with tempfile.TemporaryDirectory() as root:
            assert _with_ci(root, command), (
                "%r is a plausible entry point and must be discovered" % command)


def test_a_test_runner_is_refused_before_the_build_cost_is_paid():
    """`cargo test` is not a program; it will be refused by the probes stage anyway.

    Refusing it here is not an extra gate on top of the probes refusal -- it is the SAME refusal
    moved earlier, before the checkout, upload and build that nothing in the test command could
    ever make use of.
    """
    for command in ("cargo test", "npm test", "go test ./...", "pytest", "make test",
                    "npm run lint", "make check", "docker compose up"):
        with tempfile.TemporaryDirectory() as root:
            assert not _with_ci(root, command), (
                "%r is a test/lint/network command, not an entry point" % command)


def test_a_justfile_recipe_is_discovered():
    """A Justfile recipe is the maintainer's run command, with no language named."""
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "Justfile"), "w", encoding="utf-8") as handle:
            handle.write("run:\n\tcargo run --example demos\n")
        build, invoke = _justfile_recipe(root) or (None, None)
        assert invoke is not None and "just" in invoke[0], invoke


def test_a_taskfile_task_is_discovered():
    """A Taskfile `run` task is the maintainer's command, language-agnostic."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        return  # PyYAML is optional; the discovery silently skips without it
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "Taskfile.yml"), "w", encoding="utf-8") as handle:
            handle.write("version: '3'\ntasks:\n  run:\n    cmds:\n      - python main.py\n")
        _build, invoke = _taskfile_command(root) or (None, None)
        assert invoke is not None and "task" in invoke[0], invoke


def test_a_survey_does_not_carry_the_host_path_into_a_task():
    """`tests/environment.json` is shipped, and it was carrying an absolute host path.

    `RepoSurvey.to_json()` included `root` -- `/…/work/scratch/frf-repo-h6gn63uu` on the machine
    that did the sourcing. Two emitted repo tasks were found carrying it, which told a reader the
    workspace layout, the directory naming and the account that produced the task.

    Nothing downstream reads it: the emitted verifier resolves against the candidate root it is
    handed. Delivery review treats a workspace absolute path in an outbound artefact as blocking on
    its own, so this is asserted rather than left to a reviewer to notice.
    """
    import json

    from frf.source.repo_survey import RepoSurvey

    survey = RepoSurvey(root="/data/somebody/workdir/work/scratch/frf-repo-abcd1234",
                        languages=("go",), build_markers=("go.mod",))
    shipped = survey.to_json()

    assert "root" not in shipped, shipped
    blob = json.dumps(shipped)
    assert "/data/somebody" not in blob and "frf-repo-abcd1234" not in blob, blob
    # The parts the verifier actually needs survive.
    assert shipped["languages"] == ["go"]
    assert shipped["build_markers"] == ["go.mod"]


def test_a_go_program_is_found_whatever_its_entry_file_is_called():
    """Go names the entry file by convention, not by rule; only `package main` is load-bearing.

    Matching `main.go` rejected real command-line programs as having no entry point at all. goawk --
    an AWK implementation whose entire purpose is to be run -- declares its main in `goawk.go`, and
    93 of 107 repo `could-not-specify` refusals carried this message.

    Both halves of the declaration are required: `package main` alone appears in files holding
    helpers for the real entry point, and `func main` alone appears in comments and strings.
    """
    import os
    import tempfile

    from frf.scales.repo import _discover_entrypoint, _has_go_main

    def make(files):
        root = tempfile.mkdtemp()
        for name, body in files.items():
            os.makedirs(os.path.join(root, os.path.dirname(name)) or root, exist_ok=True)
            with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                handle.write(body)
        return root

    program = make({"go.mod": "module github.com/benhoyt/goawk\n",
                    "goawk.go": "package main\n\nfunc main() {\n\tprintln(1)\n}\n"})
    assert _has_go_main(program)
    assert _discover_entrypoint(program)[1], "a Go program must be runnable whatever its file is called"

    # `main.go` obviously still works.
    assert _has_go_main(make({"go.mod": "module x\n",
                              "main.go": "package main\nfunc main(){}\n"}))

    # A library declares no main and must stay refused -- the process seam has nothing to run.
    assert not _has_go_main(make({"go.mod": "module x\n", "lib.go": "package lib\nfunc F(){}\n"}))
    # `package main` without an entry point is a helper file, not a program.
    assert not _has_go_main(make({"go.mod": "module x\n", "helper.go": "package main\nfunc h(){}\n"}))
    # Test files are skipped, because `go build` skips them.
    assert not _has_go_main(make({"go.mod": "module x\n",
                                  "x_test.go": "package main\nfunc main(){}\n"}))


def test_repo_build_steps_run_as_root_and_still_drop_privilege():
    """The generic Dockerfile ends at `USER nobody`; a repo task appends its project install.

    Installing a Python project writes console entry points into /usr/local/bin, and as `nobody`
    that is `PermissionError: [Errno 13] Permission denied: '/usr/local/bin/json-playground'` --
    exit 1, no image. Five repo tasks in one corpus shipped an image that could not be built at all.
    The build needs root; the submission must not have it.
    """
    import re
    from frf.scales import repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index('"", "USER root", "COPY . /app"'):]
    block = block[:block.index("open(dockerfile,")]

    assert "USER root" in source, "the appended build steps must run privileged"
    assert re.search(r'"USER nobody"', block), \
        "and privilege must be dropped again before the submission runs"
    assert block.index("chown -R nobody") < block.index('"USER nobody"'), \
        "the workspace is chowned before the user is dropped, or the submission cannot write it"


def test_an_entry_point_the_build_did_not_install_is_refused_not_observed():
    """`command -v` failing used to fall through, and what followed was exit 127 on every scenario.

    That is perfectly reproducible: five freeze runs agree, every channel freezes, `ceiling` scores
    the reference 100% against its own frozen failure, and the task ships grading a submission on
    reproducing "command not found". Fourteen of twenty-five attested repo tasks in one corpus were
    exactly that. The build ran and did not provide the entry point -- a fact about the repository,
    seen here at the cheapest point where it can be seen at all.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index('def _build_remote'):]
    block = block[:block.index("\n    def ", 10)]

    assert "command -v" in block
    assert "BuildFailed" in block.split("command -v", 1)[1], \
        "a program the build did not install must be refused, not observed as 127s"


def test_a_node_project_is_allowed_to_fetch_what_it_declares():
    """`npm install --offline` in a fresh sandbox installs nothing: the cache is empty.

    The project's own build then fails on its own devDependencies -- `sh: 1: ts-node: not found`,
    `sh: 1: vitest: not found` -- and six of eighteen repo build failures in one batch were exactly
    that, each reading as a repository that would not build when the repository was fine.

    Building a task may reach the network; the SUBMISSION may not. They are different claims.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    installs = [line for line in source.splitlines()
                if '"npm", "install"' in line]
    assert installs, "the node entry-point discovery must still install dependencies"
    for line in installs:
        assert "--offline" not in line, \
            "a fresh sandbox has no npm cache, so --offline installs nothing: %s" % line.strip()


def test_a_node_start_script_is_built_before_it_is_started():
    """`start` usually means "start what build produced".

    A Next.js or Vite project declares both, and `npm run start` without `npm run build` finds no
    artefact. The freeze sees one failure and the delivered image sees another, and the two agree on
    exactly half the channels: stdout is empty and the tree unchanged in both, while the exit code
    and stderr are not. Five repo tasks were refused at precisely 34/68, 90/180, 114/228, 96/192
    and 136/272 -- every one of them 50%.
    """
    import json
    import tempfile
    from frf.scales.repo import _discover_entrypoint

    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": "thing", "scripts": {"start": "next start", "build": "next build"}},
                      handle)
        build, invoke = _discover_entrypoint(root)

    assert invoke == ["npm", "run", "start"]
    assert ["npm", "run", "build"] in build, \
        "a project that declares a build must have it run before start: %r" % (build,)


def test_the_smoke_gate_keeps_what_works_instead_of_refusing_what_does_not():
    """It ran three scenarios and checked only that nothing RAISED.

    An invocation the program does not accept does not raise: it prints usage to stderr, writes
    nothing to stdout, touches no file and exits 2. 76 of 82 repo tasks in one corpus were made
    entirely of those -- reproducible, gradeable, and measuring nothing.

    Refusing the whole candidate when three samples fail is the other error. A repository yields
    scenarios by guessing how its program is invoked, and guessing wrongly about most of them says
    nothing about the rest: forty lifted and eight that run is a task.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index("KEEP WHAT WORKS"):]
    block = block[:block.index("# The scenario corpus is the concrete contract")]

    assert "did_work" in block, "the gate must look at what came back, not only that it did"
    assert "working.append(scenario)" in block, "and keep the scenarios that did something"
    assert "ran but did nothing" in block, "refusing only when NOTHING worked"
    assert "SMOKE_MAX_SECONDS" in block, "bounded, or a slow repository spends the freeze's budget"


def test_one_definition_of_having_done_work():
    """The smoke gate asks it of one run, the freeze asks it of a corpus.

    Two definitions would drift, and the drift would be silent: a corpus could pass the cheap gate
    and be discarded by the expensive one, or worse, the other way round.
    """
    from frf.observe.process import observation, stages
    import frf.scales.repo as repo_module

    assert callable(observation.did_work)
    for module in (stages, repo_module):
        source = open(module.__file__, encoding="utf-8").read()
        assert "def did_work" not in source and "def _did_work" not in source, \
            "%s defines its own copy of the rule" % module.__name__


def test_a_program_that_printed_nothing_and_failed_did_not_work():
    from frf.observe.process.observation import did_work

    class _Stream:
        def __init__(self, text=""):
            self.lines = tuple(text.splitlines())

    class _Observed:
        def __init__(self, code, out):
            self.exit_code, self.stdout = code, _Stream(out)

    assert not did_work(_Observed(2, "")), "usage error with no output is not work"
    assert did_work(_Observed(0, "")), "a clean exit is work even with no stdout -- it wrote files"
    assert did_work(_Observed(1, "3 problems found\n")), "output is work even when the exit is not 0"


def test_sourcing_asks_whether_a_repository_declares_a_command():
    """Two thirds of what a topic search returns are libraries, and this scale needs programs.

    Each was refused at `no discoverable entry point` AFTER a checkout. Filtering does not deliver
    fewer candidates -- `walk` continues until it has yielded the budget it was asked for -- so it
    delivers different ones, drawn deeper from a pond of eleven star segments per query.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index("def keep(candidate"):]
    block = block[:block.index("return sourcing.walk")]

    assert "_declares_a_command" in block, "the filter must run before a candidate is cloned"
    assert "answer is False" in block, \
        "an unanswered check must not reject: None is 'we could not ask', not 'no'"


def test_an_unanswerable_check_does_not_reject_a_candidate(monkeypatch):
    """A private repository, a rate limit and an exhausted request budget all answer None.

    Refusing on those would narrow the supply whenever GitHub was having a bad minute -- and would
    do it silently, since the candidate simply never appears.
    """
    import frf.scales.repo as repo_module

    monkeypatch.setattr(repo_module, "_declares_a_command", lambda *a, **k: None)
    scale = repo_module.Repo()

    kept = []

    class _Index:
        name = "test"

        def page(self, number, *, size):
            from frf.core.scale import Candidate
            if number:
                return []
            return [Candidate("github:o/r@abc", "repo", "go", "test",
                              detail={"identity": "o/r", "commit": "abc",
                                      "repository": "https://github.com/o/r", "size_kb": 10})]

        def total(self):
            return 1

    scale._index = _Index()
    kept = list(scale.find(1))
    assert len(kept) == 1, "an unanswered check must leave the candidate in the walk"


def test_a_python_entry_point_names_the_interpreter_that_exists():
    """Debian-based images provide `python3` and no `python` at all.

    Naming the wrong one fails a long way from here: `command -v python` finds nothing after the
    build, and the entry-point check then reports that the PROJECT declares a command its own build
    does not install. Eleven of twenty-two build failures in one batch were this single word, every
    one charged to the repository.
    """
    import tempfile
    from frf.scales.repo import _discover_entrypoint

    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "main.py"), "w", encoding="utf-8") as handle:
            handle.write("print('hi')\n")
        build, invoke = _discover_entrypoint(root)

    assert invoke[0] == "python3", "the image has python3 and no python: %r" % (invoke,)


def test_invocations_are_lifted_from_the_readme_as_well_as_from_scripts(tmp_path):
    """A README's fenced blocks are the maintainer's own worked examples.

    Scripts were scanned and prose was not, which leaves out the one file every project has and
    writes for humans. Thirteen candidates in one batch lifted scenarios, tried every one, and had
    none that did anything -- `ran but did nothing`. A tool that needs a subcommand cannot be
    invoked by guessing, and the README is where the subcommand is written.
    """
    from frf.source.repo_harvest import harvest_files

    (tmp_path / "README.md").write_text(
        "# thing\n"
        "\n"
        "Run thing on a file to convert it.\n"     # prose that mentions the program
        "\n"
        "```sh\n"
        "$ thing convert input.csv\n"
        "```\n",
        encoding="utf-8")

    lifted = harvest_files(str(tmp_path), ("thing",))

    argvs = [list(item.argv) for item in lifted]
    assert ["thing", "convert", "input.csv"] in argvs, argvs
    assert not any("Run" in argv for argv in argvs), \
        "a sentence mentioning the program is not an invocation of it"


def test_a_shell_prompt_is_not_part_of_the_command(tmp_path):
    from frf.source.repo_harvest import harvest_files

    (tmp_path / "README.md").write_text("```\n% tool --check file.txt\n```\n", encoding="utf-8")
    lifted = harvest_files(str(tmp_path), ("tool",))
    assert [list(x.argv) for x in lifted] == [["tool", "--check", "file.txt"]]


def test_invocations_are_lifted_from_ci_workflows(tmp_path):
    """A workflow's `run:` steps are invocations the maintainer relies on passing.

    They carry the arguments actually used, and stay correct because they break the build when they
    are not. Thirty-one candidates in one batch were refused at `corpus-too-thin` within striking
    distance of the threshold -- thirteen at exactly nine scenarios -- and supply is what that needs.
    """
    from frf.source.repo_harvest import harvest_files

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: run thing on everything\n"          # mentions the program, is not an invocation
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - run: thing check fixtures/a.json\n"
        "      - name: many\n"
        "        run: |\n"
        "          thing convert fixtures/b.csv\n"
        "          thing lint fixtures/c.txt\n"
        "      - uses: actions/checkout@v4\n",
        encoding="utf-8")

    argvs = [list(x.argv) for x in harvest_files(str(tmp_path), ("thing",))]

    assert ["thing", "check", "fixtures/a.json"] in argvs, argvs
    assert ["thing", "convert", "fixtures/b.csv"] in argvs, "the block form must be read too"
    assert ["thing", "lint", "fixtures/c.txt"] in argvs
    assert not any("name:" in " ".join(argv) or argv[0] == "name" for argv in argvs), \
        "a `name:` that mentions the program is not a way to invoke it"


def test_a_project_is_installed_with_what_it_needs():
    """`--no-deps` installed the project and nothing it imports.

    The delivered image then held a program that could not start, and the pipeline recorded that the
    PROJECT did not work. Measured over 324 attempts: Python repo candidates built at 8% and node at
    4%, against Go's 100% -- which is not a fact about Python or JavaScript.

    Building a task may reach the network; the submission it produces may not. Those are different
    claims and only the second is a property of the corpus.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()

    installs = [line.strip() for line in source.splitlines()
                if "pip install" in line and "RUN" in line]
    assert installs, "a python repo task must install its project"
    first = installs[0]
    assert "--no-deps" not in first.split("||")[0], \
        "the first attempt must bring the dependencies: %s" % first

    npm = [line for line in source.splitlines() if '"npm", "install"' in line]
    assert npm, "a node repo task must install its project"
    for line in npm:
        assert "--ignore-scripts" not in line, (
            "refusing `prepare` while running `npm run build` protects nothing and breaks every "
            "project that builds itself in prepare: %s" % line.strip())


def test_npm_run_build_is_only_used_when_the_project_declares_it(tmp_path):
    """`npm run build` was added unconditionally and fails outright when absent.

    `npm error Missing script: "build"`, exit 1, and the repository is recorded as one that will not
    build. It is the branch every package with a `bin` takes -- which is every candidate the repo
    scale wants -- and node built at 0% while it was there.
    """
    import json
    from frf.scales.repo import _discover_entrypoint

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "thing", "bin": {"thing": "cli.js"}, "scripts": {"test": "jest"}}),
        encoding="utf-8")
    build, invoke = _discover_entrypoint(str(tmp_path))

    assert ["npm", "install"] in build
    assert ["npm", "run", "build"] not in build, \
        "the project declares no build script: %r" % (build,)

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "thing", "bin": {"thing": "cli.js"},
                    "scripts": {"build": "tsc"}}),
        encoding="utf-8")
    build, _ = _discover_entrypoint(str(tmp_path))
    assert ["npm", "run", "build"] in build, "and it must be run when the project does declare one"


def test_a_node_bin_is_invoked_by_its_file_not_its_name(tmp_path):
    """`npm install` links a package's own `bin` into `./node_modules/.bin/`, not onto PATH.

    So `command -v redos-detector` finds nothing after a successful build, and the project is
    recorded as declaring a command its own build does not install. Ten distinct node candidates
    were refused that way in one batch, every one of them fine.
    """
    import json
    from frf.scales.repo import _discover_entrypoint

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "redos-detector", "bin": {"redos-detector": "./dist/cli.js"},
                    "scripts": {"build": "tsc"}}),
        encoding="utf-8")

    build, invoke = _discover_entrypoint(str(tmp_path))

    assert invoke == ["node", "{ROOT}/dist/cli.js"], invoke
    assert ["npm", "install"] in build and ["npm", "run", "build"] in build


def test_the_harvest_searches_for_the_name_the_project_publishes(tmp_path):
    """A node bin is invoked as `node dist/cli.js` -- correct, because npm does not put it on PATH.

    Every README and workflow says `redos-detector`. Searching only for our spelling found nothing,
    the harvest returned empty, and the scale fell back to feeding the program each file in the
    repository: `{PROGRAM} .eslintrc.js`, `{PROGRAM} README.md`. How we invoke it and what it is
    called are different facts.
    """
    import json
    from frf.scales.repo import _declared_names

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "redos-detector", "bin": {"redos-detector": "./dist/cli.js"}}),
        encoding="utf-8")
    assert "redos-detector" in _declared_names(str(tmp_path))

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'thing'\n\n[project.scripts]\nthing-cli = \"thing.main:run\"\n",
        encoding="utf-8")
    assert "thing-cli" in _declared_names(str(tmp_path))


def test_a_documented_placeholder_is_not_a_command(tmp_path):
    """A README shows the SHAPE of a command, and that is not a command.

    `tool --log_file <path_to_log_file>` names a file that does not exist: the program exits 2 and
    the scale spends a smoke run finding out. Four of thirteen invocations lifted in one batch were
    templates like this, and they crowded out the ones that would have run.
    """
    from frf.source.repo_harvest import harvest_files

    (tmp_path / "README.md").write_text(
        "```\n"
        "tool --interval <ms> --log_file <path_to_log>/out.txt\n"   # placeholder
        "tool [CMD] path_to_binary\n"                                # placeholder
        "tool = 0.1.0\n"                                             # a dependency line
        "tool FILE\n"                                                # manual-style placeholder
        "tool convert samples/a.csv\n"                               # a real one
        "```\n", encoding="utf-8")

    argvs = [list(x.argv) for x in harvest_files(str(tmp_path), ("tool",))]

    assert argvs == [["tool", "convert", "samples/a.csv"]], argvs


def test_one_documented_invocation_is_generalised_over_the_project_corpus():
    """A corpus needs about ten scenarios and a project rarely documents ten commands.

    It usually documents ONE and ships a directory of inputs -- the whole shape of a transformer,
    and what this scale sources for. `corpus-too-thin` refused 34 candidates in one batch, most of
    them far below the floor.

    The shape stays the maintainer's; only the argument moves, and every substitution is still
    proved by the same pre-freeze pass as the original.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index("ONE WORKING SHAPE, MANY INPUTS"):]
    block = block[:block.index("return tuple(scenarios)")]

    assert "siblings" in block and "SIBLING_SCENARIOS" in block
    assert "endswith" in block, "a sibling must share the documented argument's kind"
    assert repo_module.SIBLING_SCENARIOS >= 10, \
        "ten scenarios of four channels is what the graded-point floor needs"


def test_the_scenario_match_knows_the_declared_names_too():
    """The search was taught what the project calls its command; the match was not.

    A line lifted from a README as `redos-detector input.txt` found no token in `{node, cli.js}` and
    was dropped again -- the same regression as the search, one layer down.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index("executable_names = ("):]
    block = block[:block.index("scenarios = []")]
    assert "_declared_names" in block


def test_the_workload_fallback_prefers_the_project_own_inputs(tmp_path):
    """This sorted by path depth, so root config came first and `testdata/` was cut by the limit.

    The fallback then ran `{PROGRAM} .eslintrc.js` and `{PROGRAM} README.md`, and the candidate was
    refused for having done nothing -- a refusal that was ours. Those are not inputs, and the
    project's real inputs were in a directory named for the purpose.
    """
    from frf.scales.repo import _repo_workload_files

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / ".eslintrc.js").write_text("module.exports={}\n", encoding="utf-8")
    data = tmp_path / "testdata"
    data.mkdir()
    (data / "a.json").write_text('{"a":1}', encoding="utf-8")
    (data / "b.json").write_text('{"b":2}', encoding="utf-8")

    files = _repo_workload_files(str(tmp_path))

    assert files[:2] == ["testdata/a.json", "testdata/b.json"], files
    assert "package.json" not in files and "README.md" not in files
    assert ".eslintrc.js" not in files, "a dotfile configures the repository, it is not a workload"


def test_the_shipped_wrapper_starts_the_subject_the_way_it_was_observed():
    """`restricted_argv` wraps the reference during the freeze and says why in its own docstring:

        the subject must be started identically for the reference and for the candidate,
        or the comparison measures the wrapper

    The shipped `run.sh` did not carry the first half of that wrapper, and the comparison measured
    the wrapper. `ENV` is the load-bearing part: this host sets `ENV=/etc/shinit_v2`, which runs
    dpkg, which writes a permission warning to STDERR -- a graded channel.

    Measured across 75 in-image refusals: 47 at precisely 50% and 19 at precisely 25%, with whole
    channels failing together. One instrumented run gave the shape exactly --
    `stdout 47/47 pass, tree 47/47 pass, exit_code 0/47, stderr 0/47`.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index('handle.write("#!/bin/sh'):]
    block = block[:block.index("os.chmod(run, 0o755)")]

    assert "unset ENV BASH_ENV" in block, "the freeze unsets these; the delivered task must too"
    assert "ulimit -u" in block, "and it is started under the same process cap"
    assert "integrity.PROCESS_CAP" in block, \
        "one number, or the two drift and the drift is a graded channel"


def test_the_verifier_does_not_ask_for_a_privilege_drop_it_cannot_make():
    """`setpriv --reuid` changes the user id, which an unprivileged process may not do.

    Run as `nobody` it fails with `setpriv: setgroups failed: Operation not permitted` and exits
    127 -- identically, on every scenario. The image already declares `USER nobody`, so the
    isolation this wrapper provides is in force before the verifier starts.

    Two isolation mechanisms collided, and the effect hid in plain sight: stdout and the file tree
    matched perfectly, because a program that never ran writes nothing and touches nothing, while
    exit code and stderr failed on all 47 scenarios. In aggregate, 47 of 75 in-image refusals sat at
    precisely 50% and 19 at precisely 25%.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index('if environment.get("isolated") and shutil.which("setpriv")'):]
    block = block[:block.index("submission_prog = [submission]")]

    assert "os.geteuid() == 0" in block, \
        "only root can drop to nobody, and only root needs to"


def test_reachability_checks_the_file_the_interpreter_was_handed(tmp_path):
    """The check was written when argv[0] WAS the program.

    An interpreted entry point is `node /app/dist/cli.js` or `python3 /app/main.py`, where argv[0]
    is a world-readable interpreter and the file that matters is the next token. Checking only the
    first made the guard pass while the subject was unreachable -- the silent failure its own
    docstring describes: every probe returns the same permission error and the freeze records it as
    the program's behaviour.

    It is why the only repo tasks that ever worked were Go: a Go entry point IS argv[0], and
    `go build` writes it 0755.
    """
    import os
    from frf.core.integrity import _reachable_by

    # Every directory above the script must be traversable too -- pytest's tmp dirs are 0700, and
    # the guard is right to refuse a path it cannot walk.
    root = tmp_path
    while str(root) != "/":
        try:
            os.chmod(root, 0o755)
        except OSError:
            break
        root = root.parent
    script = tmp_path / "cli.js"
    script.write_text("console.log(1)\n", encoding="utf-8")

    os.chmod(script, 0o644)
    assert _reachable_by("nobody", ["/bin/sh", str(script)]) is True

    os.chmod(script, 0o600)
    assert _reachable_by("nobody", ["/bin/sh", str(script)]) is False, \
        "an interpreter has to read the script it was handed"


def test_the_build_leaves_its_artefacts_readable():
    """The build runs as root and a toolchain that writes 0600 leaves an unreadable artefact.

    The isolation wrapper then drops to `nobody`, every probe returns the same permission error, and
    the freeze records that as the program's behaviour -- reproducible five times over.
    """
    import frf.scales.repo as repo_module

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index("def _build_remote"):]
    block = block[:block.index("\n    def ", 10)]
    assert "chmod -R a+rX" in block, "the built tree must be readable by whoever runs the subject"


def test_a_sandbox_build_failure_reports_its_cause():
    """A failed build ends with a frame quoting the command and a `note:` disclaiming blame.

    Both are true and useless, and both are in the tail. Eight of thirteen build failures in one
    batch read identically -- `note: This is an issue with the package mentioned above, not pip` --
    which names nothing at all.
    """
    import frf.scales.repo as repo_module
    from frf.observe.in_image import _why_it_failed

    source = open(repo_module.__file__, encoding="utf-8").read()
    block = source[source.index("def _build_remote"):]
    block = block[:block.index("\n    def ", 10)]
    assert "_why_it_failed" in block, "the sandbox build must report a cause, not a tail"

    noisy = "\n".join([
        "  Building wheel for thing",
        "  error: could not find system library 'libfoo' required by the `foo-sys` crate",
        "  " + "filler " * 300,
        "  note: This is an issue with the package mentioned above, not pip.",
        "  See above for output.",
    ])
    said = _why_it_failed(noisy)
    assert "libfoo" in said, said
    assert "not pip" not in said and "See above" not in said
