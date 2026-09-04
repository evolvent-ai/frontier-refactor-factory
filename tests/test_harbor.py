"""The shipped format -- one emitter, and the properties a task must have to be safe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.core import harbor                                           # noqa: E402


def _package(**overrides) -> harbor.Package:
    base = dict(name="example", scale="module", description="A program that does a thing.",
                instruction="Make it faster.", source_language="python",
                provenance={"origin": "https://example.invalid/repo at v1", "probes": 40,
                            "freeze_runs": 5})
    base.update(overrides)
    return harbor.Package(**base)


def test_the_verifier_directory_is_never_readable_by_the_submission():
    """`separate` is not configurable, and this is the check that keeps it that way.

    With a shared environment, tests/ is readable from inside the submission -- and tests/ holds
    every expectation and a runnable reference. Reading the answer key and replaying it would score
    full marks, which makes "do not call the reference" a request rather than a property.
    """
    text = harbor.task_toml(_package())
    assert 'environment_mode = "separate"' in text
    assert "shared" not in text.replace("# ", "", 1) or 'environment_mode = "shared"' not in text
    # And the reason travels with the setting, so nobody flips it back without reading why.
    assert "replaying" in text or "answer key" in text


def test_an_upstream_test_suite_is_not_mistaken_for_the_answer_key():
    """The false positive that was refusing real material at emit.

    `environment/` is required to be a complete checkout at a fixed revision, and most real projects
    ship a `tests/` directory. The preflight used to reject any `environment/tests` by name, so every
    candidate whose upstream had one was refused -- and charged to us as a FACTORY fault, which is the
    attribution that decides whether a low yield reads as bad material or as our bug. Nineteen of the
    hundred-and-five already-emitted tasks failed exactly this, carrying `basic_example.py` and
    `test_array.py`: the project's own tests, which belong in a faithful checkout.
    """
    with tempfile.TemporaryDirectory() as root:
        upstream = os.path.join(root, "environment", "tests")
        os.makedirs(upstream)
        for name in ("basic_example.py", "test_array.py", "__init__.py"):
            open(os.path.join(upstream, name), "w").close()
        assert harbor.answer_key_leaks(os.path.join(root, "environment")) == []


def test_the_factory_answer_key_is_caught_wherever_it_is():
    """What matters is whether the graded answers are reachable from the submission.

    Checked by content rather than by directory name, and at any depth: a copy nested three levels
    down is exactly as readable to a submission as one at the top, and a submission that can read the
    expectations can replay them for full marks.
    """
    for relative in ("tests/expectations.json", "deep/nested/verify.py",
                     "scenarios.jsonl", "reference/verify.py"):
        with tempfile.TemporaryDirectory() as root:
            environment = os.path.join(root, "environment")
            planted = os.path.join(environment, relative)
            os.makedirs(os.path.dirname(planted), exist_ok=True)
            open(planted, "w").close()
            leaks = harbor.answer_key_leaks(environment)
            assert leaks, "a submission could read %s and replay the answers" % relative
            assert "answer key" in leaks[0]


def test_a_gpu_field_exists_before_any_gpu_task_does():
    """CPU tasks write gpus = 0 rather than omitting the key.

    Emitting it always means the day a GPU task exists, nothing about this file changes -- the field
    the harness reads is already there, with the name the harness already uses.
    """
    cpu = harbor.task_toml(_package())
    assert "gpus = 0" in cpu and "gpu_types = []" in cpu

    gpu = harbor.task_toml(_package(gpus=1, gpu_types=["H100"]))
    assert "gpus = 1" in gpu and '"H100"' in gpu


def test_one_emitter_serves_every_scale():
    """The same function, four scales, no branch. `scale` is data, not a code path."""
    for scale in ("kernel", "module", "package", "repo"):
        text = harbor.task_toml(_package(scale=scale))
        assert 'scale = "%s"' % scale in text
        assert 'name = "%s/example"' % scale in text


def test_cross_language_is_derived_rather_than_declared_twice():
    """One parameter decides which family a task belongs to, so the two cannot disagree."""
    same = _package(source_language="rust", target_language="")
    assert not same.cross_language
    assert "optimisation" in harbor.task_toml(same)

    ported = _package(source_language="rust", target_language="go")
    assert ported.cross_language
    assert "cross-language" in harbor.task_toml(ported)

    # Declaring the same language as a "target" is optimisation, not a port.
    assert not _package(source_language="go", target_language="go").cross_language


def test_the_provenance_sentence_states_what_can_be_checked():
    text = harbor.task_toml(_package())
    assert "example.invalid/repo at v1" in text
    assert "40 probe(s)" in text and "5 repeated runs" in text
    assert "No expected output was hand-authored." in text


def test_the_entry_script_runs_and_writes_the_flat_reward():
    """test.sh is what the harness executes, so it is checked by executing it."""
    tmp = tempfile.mkdtemp(prefix="frf-harbor-")
    try:
        harbor.write(tmp, _package())
        tests = os.path.join(tmp, "tests")

        # A stand-in verifier that writes the detailed report the real one writes.
        with open(os.path.join(tests, "verify.py"), "w") as fh:
            fh.write('''
import json, os, sys
json.dump({"reward": 0.42, "correct": True, "correctness_passed": 7,
           "correctness_total": 7, "speedup": 1.9, "note": "ok"},
          open(os.environ["REWARD_PATH"], "w"))
sys.exit(0)
''')
        logs = os.path.join(tmp, "logs", "verifier")
        os.makedirs(logs, exist_ok=True)

        # Redirect the absolute log path into the sandbox so the script can be run for real here.
        script = open(os.path.join(tests, "test.sh")).read().replace("/logs/verifier",
                                                                     logs)
        run_me = os.path.join(tests, "run_test.sh")
        with open(run_me, "w") as fh:
            fh.write(script)
        os.chmod(run_me, 0o755)

        result = subprocess.run(["bash", run_me], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr

        flat = json.load(open(os.path.join(logs, "reward.json")))
        assert flat["reward"] == 0.42 and flat["correct"] is True
        assert flat["correctness_passed"] == 7 and flat["speedup"] == 1.9
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_verifier_that_writes_nothing_scores_zero_and_says_which_zero():
    """"The verifier failed" and "the submission was wrong" are different findings.

    Both end in a reward of zero, so the note has to distinguish them -- otherwise a broken task
    reads exactly like a bad submission, and nobody goes looking.
    """
    tmp = tempfile.mkdtemp(prefix="frf-harbor-silent-")
    try:
        harbor.write(tmp, _package())
        tests = os.path.join(tmp, "tests")
        with open(os.path.join(tests, "verify.py"), "w") as fh:
            fh.write("import sys\nsys.exit(1)\n")     # writes no report at all
        logs = os.path.join(tmp, "logs", "verifier")
        os.makedirs(logs, exist_ok=True)
        script = open(os.path.join(tests, "test.sh")).read().replace("/logs/verifier", logs)
        run_me = os.path.join(tests, "run_test.sh")
        with open(run_me, "w") as fh:
            fh.write(script)
        os.chmod(run_me, 0o755)

        result = subprocess.run(["bash", run_me], capture_output=True, text=True, timeout=120)
        assert result.returncode != 0, "the verifier's own failure must reach the harness"

        flat = json.load(open(os.path.join(logs, "reward.json")))
        assert flat["reward"] == 0.0
        assert "no report" in flat["note"], flat["note"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_provenance_sentence_never_invents_a_number():
    """It shipped "0 probe(s), distilled from 0 repeated runs" beside an instruction saying 57 and 5.

    Both came from the same package, so a reader comparing the two could not tell which was lying --
    and this sentence is the one someone quotes months later. The cause was `.get(key, 0)`: a
    default that turns "the caller forgot" into a confident, specific, wrong claim. Silence is the
    only honest answer to a number nobody supplied.
    """
    from frf.core import harbor

    complete = harbor.Package(name="n", scale="module", description="d", instruction="i",
                              source_language="python",
                              provenance={"origin": "o", "probes": 57, "freeze_runs": 5})
    said = harbor._provenance_sentence(complete)                 # noqa: SLF001
    assert "57 probe(s)" in said and "5 repeated runs" in said

    for missing in ({"origin": "o"},
                    {"origin": "o", "probes": 57},
                    {"origin": "o", "freeze_runs": 5}):
        bare = harbor.Package(name="n", scale="module", description="d", instruction="i",
                              source_language="python", provenance=missing)
        said = harbor._provenance_sentence(bare)                  # noqa: SLF001
        assert "were not recorded" in said, said
        assert "0 probe" not in said, "a missing number must not be reported as zero: %s" % said


def test_the_candidate_is_not_root_and_still_has_a_usable_home():
    """Dropping to `nobody` without a HOME silently ends every compiled task.

    `nobody`'s home is `/nonexistent`, and every compiled toolchain caches under $HOME. Verified by
    building the generated image and running as the dropped user:

        failed to initialize build cache at /nonexistent/.cache/go-build: permission denied

    So the two lines belong together: a task image that drops the user and does not give it a
    writable home is worse than one that never dropped it, because the failure looks like a broken
    toolchain rather than a permissions decision.

    The workspace and the verifier log directory are chowned for the same reason -- the candidate
    was told to work in `/app`.
    """
    from frf.core.harbor import dockerfile_for

    for language in ("python", "go", "rust", "javascript"):
        text = dockerfile_for(language, "")
        assert "USER nobody" in text, language
        assert "ENV HOME=/home/candidate" in text, language
        assert "/home/candidate" in text and "chown -R nobody:nogroup" in text, language
        # HOME must be set and the directory owned BEFORE the user is dropped, or the drop lands
        # on a home the candidate cannot write.
        assert text.index("chown -R nobody:nogroup") < text.index("USER nobody"), language
        assert text.index("WORKDIR /app") < text.index("USER nobody"), language


def test_a_global_npm_install_does_not_collide_with_the_base_image():
    """The official node images ship a yarn at /usr/local/bin/yarn.

    npm 9 refuses to overwrite a binary it does not own -- `npm error File exists:
    /usr/local/bin/yarn`, exit 1, image not built. It was the single largest defect in a finished
    corpus: about forty JavaScript and TypeScript tasks whose delivered image could not be built at
    all. The production run did not notice because the production container is not the delivered
    image, which is the deeper lesson; this is the immediate one.
    """
    from frf.core.shims.dockerfiles import _LANGUAGE_SETUP as LANGUAGES

    for language in ("javascript", "typescript"):
        for command in LANGUAGES[language]["install_cmds"]:
            if "npm install -g" in command and "yarn" in command:
                assert "--force" in command, \
                    "%s installs yarn over the base image's own without --force: %r" % (
                        language, command)


def test_a_language_floor_moves_in_every_place_it_is_written():
    """A toolchain version appears twice: as the base image, and as a cross-language install.

    Raising one and not the other moves the floor for tasks where the language is the SOURCE and
    leaves it for tasks where it is the TARGET, which is the harder half to notice.
    """
    import re
    from frf.core.shims.dockerfiles import _LANGUAGE_SETUP as LANGUAGES

    for language in ("rust",):
        setup = LANGUAGES[language]
        base = re.search(r"(\d+)\.(\d+)", str(setup["base_image"]))
        assert base, setup["base_image"]
        for command in setup["install_cmds"]:
            pinned = re.search(r"--default-toolchain\s+(\d+)\.(\d+)", command)
            if not pinned:
                continue
            assert pinned.groups() == base.groups(), (
                "%s installs %s as a cross-language target while its base is %s"
                % (language, pinned.group(0), setup["base_image"]))


def test_a_toolchain_is_not_installed_over_its_own_base_image():
    """`rust:1.90-bookworm` ships cargo and rustc; rustup-init on top of it fails outright.

    `error: cannot install while Rust is installed`, exit 1, no image -- the same shape as
    `npm install -g yarn` against a node base that already has one. The fetch command is still
    needed for the other case, where rust is a cross-language TARGET on somebody else's base.
    """
    from frf.core.harbor import dockerfile_for
    from frf.core.shims.dockerfiles import _LANGUAGE_SETUP as LANGUAGES

    assert LANGUAGES["rust"]["install_cmds"] == [], \
        "the base image is the toolchain; installing it again is what fails"
    assert LANGUAGES["rust"]["cross_install_cmds"], \
        "and it is still needed where rust is a target on another base"

    inplace = dockerfile_for("rust", "")
    assert "rustup" not in inplace and "sh.rustup.rs" not in inplace, inplace

    cross = dockerfile_for("python", "rust")
    assert "sh.rustup.rs" in cross, "a cross-language target must still be fetched"


def test_go_fetches_the_toolchain_its_go_mod_declares():
    """The floor went 1.23 -> 1.25 -> 1.26 in two days, which is a rule that will keep being wrong.

    Pinning GOROOT makes Go refuse to switch toolchains, so a repository whose go.mod declares a
    newer version fails outright: `go.mod requires go >= 1.26.0 (running go 1.25.14;
    GOTOOLCHAIN=local)`. Fetching stays deterministic -- go.mod names the exact version -- and costs
    outbound HTTP at build time, which is allowed; the submission is what must need no network.
    """
    from frf.core.harbor import dockerfile_for
    from frf.core.shims.dockerfiles import _LANGUAGE_SETUP as LANGUAGES

    assert LANGUAGES["go"]["env"].get("GOTOOLCHAIN") == "auto"
    assert "GOTOOLCHAIN=auto" in dockerfile_for("go", "")


def test_the_image_carries_what_a_native_build_links_against():
    """`build-essential` gives a compiler and nothing else.

    A third of one batch's build failures were a package missing beneath it: `Make sure you also
    have the development packages of openssl installed` (6 of 33) and `llvm-sys` refusing to compile
    without LLVM (5 of 33). These are the same short list every time, and installing them once is
    cheaper than refusing a repository for each one and calling it unbuildable material.
    """
    from frf.core.harbor import dockerfile_for

    text = dockerfile_for("rust", "")
    for package in ("pkg-config", "libssl-dev", "libclang-dev", "llvm-dev", "cmake"):
        assert package in text, "%s is missing: %s" % (package, text)


def test_the_javascript_image_carries_the_package_managers_projects_declare():
    """`bun: not found` was named in a comment as a cause of most of one batch's refusals.

    Then pnpm and yarn were installed and bun was not -- half a fix, left long enough to become the
    single largest build failure the repo scale had: fifteen of twenty-four. It also carries the
    `workspace:` protocol that npm refuses with EUNSUPPORTEDPROTOCOL.
    """
    from frf.core.shims.dockerfiles import _LANGUAGE_SETUP as LANGUAGES

    for language in ("javascript", "typescript"):
        installs = " ".join(LANGUAGES[language]["install_cmds"])
        for manager in ("pnpm", "yarn", "bun"):
            assert manager in installs, "%s does not install %s: %s" % (language, manager, installs)
