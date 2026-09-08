"""What a call-seam task actually ships, and the verifier that grades it.

`harbor.py` lays out the parts every task has -- task.toml, the instruction, the entry script. This
writes the parts only this seam can write: the frozen expectations, the probes that produced them,
the reference the package is graded against, and a `verify.py` that speaks the same wire the factory
froze over.

WHY THE VERIFIER IS WRITTEN OUT RATHER THAN IMPORTED. The task runs in a container that has this
package and nothing else -- no `frf`, no wheel, no network. A verifier that imported the factory
would be a task that only grades on a machine with the factory installed, which is not a task, it is
a demonstration. So the file below is self-contained and is copied in whole.

WHAT IT MAY NOT CONTAIN, and this is the half that is easy to lose. The expectations are DIGESTS.
The verifier can tell whether a submission's answer matches; it cannot tell anyone what the answer
was, and nor can anything in `tests/`. That is what makes `environment_mode = "separate"` a defence
rather than a decoration -- if the plaintext answers were here, the mode would only be hiding a file
that should never have existed.

THE REFERENCE SHIPS TOO, in `tests/`, which is the one place the submission cannot read. It has to:
E7 drives the emitted package with the reference the package itself ships, and the speed comparison
needs something to compare against. On the solver's side of the wall there is only the statement.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from ..call import shims
from ...core import timing
from ..replay import ReplayResult

# What the verifier is called inside the task, and where its inputs live. Named here rather than
# spelled in three files, because a rename that misses one produces a task that fails at grading
# time with a missing-file error and no indication of which half is wrong.
VERIFIER = "verify.py"
EXPECTATIONS = "expectations.json"
REFERENCE_DIR = "reference"


def coverage_manifest(corpus, material) -> dict:
    """Count exposed operations, never stringify a numeric input as an operation name."""
    is_package = hasattr(material, "entry_points")
    operations = set(getattr(material, "entry_points", ()))
    counts = {}
    if is_package:
        for probe in corpus.inputs.values():
            if probe and isinstance(probe[0], str) and probe[0] in operations:
                name = probe[0]
                counts[name] = counts.get(name, 0) + 1
    else:
        counts[getattr(material, "symbol", "entry")] = len(corpus.inputs)
    return {"operations": counts, "probe_count": len(corpus.inputs),
            "timed_count": len(corpus.timed)}


def write_tests(path: str, corpus, *, spec, material) -> None:
    """Write everything the shipped task needs in order to grade itself.

    Signature fixed by `stages.Seam`, which calls `write_tests(path, corpus)`; the two extra
    arguments are bound by the caller. They are the spec and the located material, and they are
    needed because a package has to carry the language and the symbol -- a verifier cannot serve a
    subject it cannot name.
    """
    tests = os.path.join(path, "tests")
    os.makedirs(tests, exist_ok=True)

    frozen = {
        "symbol": getattr(material, "symbol", "entry"),
        "language": spec.language,
        "package": bool(hasattr(material, "entry_points")),
        "entry_points": list(getattr(material, "entry_points", ())),
        "dispatch": list(getattr(material, "dispatch", ())),
        "package_name": getattr(material, "package_name", ""),
        "freeze_runs": max((e.runs for e in corpus.expectations), default=0),
        # DIGESTS ONLY. See the module docstring: an expectation holding the plaintext answer is one
        # filesystem mistake away from being a key a submission can replay.
        "graded": [e.to_json() for e in corpus.expectations],
        "probes": {probe_id: args for probe_id, args in corpus.inputs.items()},
        "timed": list(corpus.timed),
        "timed_expectations": dict(getattr(corpus, 'timed_expectations', {})),
    }
    target = getattr(spec, 'target_language', '') or spec.language
    cross = target.lower() != spec.language.lower()
    if cross:
        from ..target_build import target_plan
        target_plan(target)
        frozen['target_language'] = target.lower()
        frozen['target_helpers'] = list(getattr(spec, 'environment', {}).get('target_helpers') or [])
    policy = getattr(spec, 'environment', {}).get('numeric_policy')
    if policy:
        from ..compare.numeric import validate_numeric_policy
        if policy != getattr(corpus, 'numeric_policy', None):
            raise ValueError('numerical policy changed after freeze')
        if any(pid not in frozen['timed_expectations'] for pid in corpus.timed):
            raise ValueError('numerical timing workloads lack frozen expectations')
        frozen['comparison'] = 'envelope'
        frozen['numeric_policy'] = dict(validate_numeric_policy(policy))
    with open(os.path.join(tests, EXPECTATIONS), "w", encoding="utf-8") as handle:
        json.dump(frozen, handle, indent=1, sort_keys=True)
    # Machine-readable API coverage is part of the task artifact, so audits can distinguish a
    # broad package task from one that merely repeats a single operation.
    with open(os.path.join(tests, "coverage_manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(coverage_manifest(corpus, material), handle, indent=2, sort_keys=True)

    # Package material is a checkout with a dispatch adapter, not one source file. It gets its own
    # layout while the verifier and digest format remain shared with module/kernel.
    reference = os.path.join(tests, REFERENCE_DIR)
    shim = shims.load(spec.language)
    if hasattr(material, "entry_points"):
        _serve_package_here(reference, shim, material, language=spec.language)
    else:
        _serve_here(reference, shim, material, language=spec.language)

    with open(os.path.join(tests, VERIFIER), "w", encoding="utf-8") as handle:
        handle.write(verifier_source())

    if cross:
        from ..target_workspace import write_target_workspace
        from ...core.source_tree import copy_tree
        environment = os.path.join(path, 'environment')
        original = os.path.join(environment, 'original')
        if hasattr(material, 'entry_points'):
            copy_tree(material.root, original, ignore=shutil.ignore_patterns('.git', '.hg', '__pycache__'))
            if material.package_root and material.package_name:
                package_destination = os.path.join(original, material.package_name)
                copy_tree(material.package_root, package_destination, dirs_exist_ok=True,
                          ignore=shutil.ignore_patterns('.git', '.hg', '__pycache__'))
        else:
            os.makedirs(original, exist_ok=True)
            shutil.copy2(material.source_path, os.path.join(original, os.path.basename(material.source_path)))
        write_target_workspace(environment, target.lower())
    elif hasattr(material, "entry_points"):
        _serve_package_here(os.path.join(path, "environment"), shim, material, language=spec.language)
    else:
        _serve_here(os.path.join(path, "environment"), shim, material, language=spec.language)

    context_path = os.path.join(path, "environment", "task-interface.json")
    with open(context_path, encoding="utf-8") as handle:
        context = json.load(handle)
    context["interface"] = (
        'Run /app/run.sh as a JSON-lines service. Requests contain id, op="run", '
        'call="entry", and args (an array of positional arguments). Return one JSON object '
        'per request, preserving id: {"id": id, "ok": true, "value": result} or '
        '{"id": id, "ok": false, "error": message}. Do not write diagnostics to the response stream.')
    context["symbol"] = getattr(material, "symbol", "entry")
    if cross:
        context['interface'] += ' ' + context['build_contract']
    if policy:
        from ..compare.numeric import numeric_policy_text
        context['numeric_policy'] = policy
        context['interface'] += ' ' + numeric_policy_text(policy)
    if hasattr(material, "entry_points"):
        context["operations"] = list(material.entry_points)
        context["dispatch"] = list(material.dispatch)
        context["interface"] += (
            ' The first element of args selects the package operation; remaining elements '
            'are its positional arguments. Preserve all declared operations.')
    elif hasattr(material, "schema"):
        context["parameters"] = material.schema.to_json()
    with open(context_path, "w", encoding="utf-8") as handle:
        json.dump(context, handle, indent=2)

    # The image, not a replay-only bind mount, must contain the solver's starting source.
    dockerfile = os.path.join(path, "environment", "Dockerfile")
    if os.path.isfile(dockerfile):
        with open(dockerfile, "a", encoding="utf-8") as handle:
            handle.write("\nUSER root\nCOPY . /app\n"
                         "RUN chown -R nobody:nogroup /app\nUSER nobody\n")
    if cross:
        from ..reference_image import write_reference_image
        write_reference_image(path, spec, install=getattr(material, 'install', ()))


def _serve_package_here(room: str, shim, material, *, language: str = "python") -> None:
    """Copy package sources and a generated dispatch adapter into a call-seam workspace."""
    os.makedirs(room, exist_ok=True)
    from ...core.source_tree import copy_tree, validate_links
    if os.path.abspath(material.root) == os.path.abspath(room):
        validate_links(room)
    else:
        copy_tree(material.root, room, dirs_exist_ok=True,
                  ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "test",
                                               "fixtures", "docs"))
    package_root = material.package_root or material.root
    package_name = material.package_name
    # The package root is already copied by the first copytree when it lives inside material.root.
    # Only copy it separately when the adapter material points outside that tree.

    if package_name and os.path.isdir(package_root):
        destination = os.path.join(room, package_name)
        if os.path.abspath(package_root) != os.path.abspath(destination):
            copy_tree(package_root, destination, dirs_exist_ok=True)
    # THE DISPATCHER IS GENERATED IN ONE PLACE, and this was the second copy of it. What stood here
    # was `if language in ("javascript", "typescript")` writing JS inline, and everything else falling
    # through to a PYTHON dispatcher written to subject.py -- which is precisely the fault
    # `observe/call/dispatch.py` was created to end, and says so in its own docstring: "The scale used
    # to decide with `native = language in ("javascript", "typescript")`, which quietly sent the other
    # six down the Python branch."
    #
    # `scales/package.py` was fixed to call the generator; this path was not, so the build tree got a
    # correct dispatcher and the EMITTED task got a Python one. A ruby package task would have shipped
    # `import importlib` in subject.py and failed replay for a reason that reads like broken material.
    # Now a language with no dispatcher raises Unsupported here, loudly, as it does everywhere else.
    from . import dispatch as call_dispatch

    # ONE TUPLE SHAPE FOR EVERY LANGUAGE: (module, symbol, klass, typed). Dynamic languages leave
    # klass and typed empty; ruby carries klass; static languages carry klass-empty and typed
    # (params + result) for the dispatcher's converter. A single shape means the generators unpack
    # the same way and a new language cannot fall through a missing field.
    table = {entry["name"]: ((entry["module"], entry["symbol"], entry.get("klass") or "",
                              entry.get("params") or (), entry.get("result") or {}))
             for entry in material.dispatch}
    adapter = os.path.join(room, shims.TEMPLATES[language].subject)
    with open(adapter, "w", encoding="utf-8") as handle:
        handle.write(call_dispatch.source(language, table))
    _serve_here(room, shim, type("AdapterMaterial", (), {"source_path": adapter, "symbol": "entry"})(),
                language=language)


def _serve_here(room: str, shim, material, *, language: str) -> None:
    """Lay out one servable copy of the subject: the source, the shim, and a run.sh.

    BOTH SIDES ARE LAID OUT THE SAME WAY, by the same function. The reference in tests/ and the
    solver's starting point in environment/ are byte-identical, which is what makes the timing
    comparison meaningful -- a reference served differently from the candidate would be measuring
    the difference between two harnesses.

    `language` is taken rather than inferred from `shim`: a Shim is a row of data with no name of its
    own, and two languages legitimately share one -- typescript uses serve.js, cpp uses serve.c.
    """
    import shlex

    # LAID OUT BY THE SAME FUNCTION THAT LAYS OUT THE BUILD TREE, which is what the docstring above
    # claims and what this did not do. It used to copy the subject and write the shim itself, so an
    # emitted package was missing everything `materialise` had learned to add: for a static language,
    # the generated bridge that declares the shim's entry point, and the package clause reconciled to
    # the shim's own. The reference then FROZE and PASSED EVIDENCE in the build tree and failed E7
    # replay out of the package, as `the submission exited without answering` -- a subject.go saying
    # `package dynamic` beside a serve.go saying `package main`, with no bridge.go at all.
    #
    # This is exactly the class of fault `drive` exists to catch: a reference that was built somewhere
    # the package does not contain.
    binding = getattr(material, "binding", None)
    shims.materialise(room, language, material.source_path, material.symbol, binding=binding)

    # RESOLVED AGAINST ".", not against `room`: run.sh ships inside the package and is started by the
    # verifier after a `cd` to its own directory, so absolute paths from this machine would name
    # directories the solver never had. `bridged` is passed rather than probed for the same reason --
    # there is no bridge at `./bridge.go` on THIS host to look for.
    build, argv = shim.commands(".", material.symbol, bridged=bool(binding and shim.bridge))
    lines = ["#!/usr/bin/env bash",
             "# Serve the subject over the wire the verifier speaks. Replace the implementation,",
             "# not this file: the verifier starts it exactly as written.",
             "set -euo pipefail",
             'cd "$(dirname "$0")"']
    # A compiled language builds on first run, in the container, from the source that ships. Doing
    # it here would put this machine's binary in the package -- see core/sandbox.py on why an
    # artefact built by the factory describes a program the solver never receives.
    for command in build:
        lines.append("%s >/dev/null" % " ".join(shlex.quote(part) for part in command))
    lines.append("exec %s" % " ".join(shlex.quote(part) for part in argv))
    run = os.path.join(room, "run.sh")
    with open(run, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(run, 0o755)
    with open(os.path.join(room, "task-interface.json"), "w", encoding="utf-8") as handle:
        json.dump({"build_commands": [shlex.join(cmd) for cmd in build],
                   "run_command": "/app/run.sh",
                   "build_directory": "/app",
                   "entry_source": "/app/" + shim.subject,
                   "runtime_argv": argv,
                   "launcher_builds": True}, handle, indent=2)


def drive(path: str, *, backend=None) -> tuple:
    """E7: drive the EMITTED package with the reference it ships. -> (passed, total).

    Everything else the pipeline checks measured the build tree. This opens what was actually
    written, which is where a whole class of fault lives -- a reference built in a scratch directory
    and never copied, an expectation frozen against a path absent from the package. Measured on an
    earlier factory, 14 of 78 emitted packages failed exactly this having passed everything else.
    """
    import subprocess

    from ...core import scratch

    if backend is not None and getattr(backend, "name", "") == "remote":
        from ..replay import ImageReplay
        return ImageReplay(backend).replay(path)

    tests = os.path.join(path, "tests")
    with scratch.temporary_directory() as logs:
        reward = os.path.join(logs, "reward.json")
        done = subprocess.run(
            [sys.executable, os.path.join(tests, VERIFIER),
             "--task-root", tests, "--workspace", os.path.join(tests, REFERENCE_DIR)],
            capture_output=True, text=True, timeout=1800,
            env=dict(os.environ, REWARD_PATH=reward))
        if not os.path.exists(reward):
            raise RuntimeError("the shipped verifier produced no report: %s"
                               % (done.stderr or done.stdout)[-500:])
        with open(reward, encoding="utf-8") as handle:
            report = json.load(handle)
    passed = int(report.get("correctness_passed", 0))
    total = int(report.get("correctness_total", 0))
    if (done.returncode != 0 or total <= 0 or report.get("timing_valid") is not True
            or passed != total):
        raise RuntimeError(report.get("note") or
                           "package reference replay mismatch (%d/%d)" % (passed, total))
    return ReplayResult(passed, total, report)


# The verifier, shipped whole. Written as a string rather than kept as a module and copied, because
# what a task contains should be readable in the file that decides what a task contains.
def verifier_source(*, isolated=True):
    from .. import isolated as confinement, transport
    from .. import target_build
    from .. import reference_image
    from ..compare import numeric
    return (VERIFIER_SOURCE.replace('__TIMING_PROTOCOL__', timing.standalone_source())
            .replace('__TRANSPORT_PROTOCOL__', transport.standalone_source())
            .replace('__ISOLATION_PROTOCOL__', confinement.standalone_source())
            .replace('__NUMERICAL_PROTOCOL__', numeric.source())
            .replace('__TARGET_BUILD__', target_build.standalone_source())
            .replace('__REFERENCE_CONTEXT__', reference_image.standalone_source())
            .replace('__USE_CONFINEMENT__', repr(isolated)))


VERIFIER_SOURCE = '''#!/usr/bin/env python3
"""Grade a submission against expectations frozen from the reference.

Self-contained on purpose: this runs in a container holding the task and nothing else.

    correctness   every graded probe's answer must digest to what the reference reproduced
    speed         then, and only then, the submission is timed against the reference

Correctness UNLOCKS speed rather than being averaged with it. Short of complete, no amount of speed
helps; past it, there is no ceiling. A partial score is still reported, because a submission missing
three probes of sixty is worth repairing and a zero would not say so.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack

__TIMING_PROTOCOL__
__TRANSPORT_PROTOCOL__
__ISOLATION_PROTOCOL__
__NUMERICAL_PROTOCOL__
__TARGET_BUILD__
__REFERENCE_CONTEXT__
USE_CONFINEMENT = __USE_CONFINEMENT__
ROOTS = {}
ACTIVE_ROOT = None
PENDING_REPORT = None
ISOLATION_REPORT = None
VERIFIER_SHA256 = hashlib.sha256(open(__file__, 'rb').read()).hexdigest()
TIMING_REPORT = {}

CALL_TIMEOUT = 30.0
TIMED_REPEATS = 200


def digest(ok, value, error):
    """The digest the factory froze with. MUST match `Observation.digest` byte for byte.

    All three fields, sorted keys, no whitespace, and the `sha256:` prefix. Every one of those is
    load-bearing and each was wrong in the first version of this file: digesting two fields instead
    of three, and dropping the prefix, produced a verifier that disagreed with every expectation it
    was given -- 0 of 57, on a package whose reference was byte-identical to the one frozen from.

    That is the duplication this file cannot avoid -- the container has no `frf` to import -- so the
    format is written out here deliberately and E7 is what keeps the two copies honest.
    """
    # Python minor versions changed the wording of this standard-library error. The operation
    # and failure class are identical, so freeze the stable semantic spelling rather than making
    # package replay depend on the interpreter used by the factory host.
    if isinstance(error, str) and error in {
            "ValueError: max() iterable argument is empty",
            "ValueError: max() arg is an empty sequence"}:
        error = "ValueError: max() arg is an empty sequence"
    if isinstance(error, str) and ("Cannot find module" in error or
                                   "module resolution failed" in error or
                                   "ERR_MODULE_NOT_FOUND" in error):
        error = "Error: module resolution failed"
    body = json.dumps({"ok": ok, "value": value, "error": error},
                      sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


class Subject:
    """A subject held open across the corpus, spoken to in JSON lines."""

    def __init__(self, argv, cwd, role='candidate'):
        self.root = ROOTS.get(role)
        if self.root is not None:
            self.activate()
            command = [str(value).replace(os.path.abspath(cwd), '/app') for value in argv]
            self.proc = self.root.spawn(command, cwd='/app', environment={'PATH': os.environ.get('PATH', '')})
        else:
            self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         start_new_session=True)
        self.transport = PipeTransport(self.proc)
        self.next_id = 0

    def activate(self):
        global ACTIVE_ROOT
        if self.root is not None and ACTIVE_ROOT is not self.root:
            for other in ROOTS.values():
                if other is not self.root:
                    other.pause()
            self.root.resume()
            ACTIVE_ROOT = self.root

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.root is not None:
            self.root.stop()
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def _exchange(self, payload):
        self.activate()
        line = self.transport.exchange(json.dumps(payload) + "\\n", timeout=CALL_TIMEOUT, line=True)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise RuntimeError("the subject returned a non-object JSON reply (%s)" %
                               type(reply).__name__)
        if reply.get("id") != payload.get("id"):
            raise RuntimeError("the submission returned a mismatched response id")
        return reply

    def call(self, args):
        self.next_id += 1
        reply = self._exchange({"id": self.next_id, "op": "run", "call": "entry", "args": args})
        return reply

    def time(self, args, repeats):
        """Seconds for `repeats` internal calls. -> (seconds, whether the subject accepted it).

        A REFUSAL IS NOT A TIMING FAILURE. The shim answers `ok: false` when the entry point raised,
        and on a subject with a guard clause that is most of its inputs -- so treating it as an error
        made every task whose held-out probes happened to be refused report "timing could not be
        completed" and silently score speedup 1.0. Refusing takes real time and is real behaviour;
        what matters is that both sides are asked the same thing and one of them is not being timed
        on a path the other never takes, which is why `accepted` travels with the number.
        """
        self.next_id += 1
        reply = self._exchange({"id": self.next_id, "op": "time", "call": "entry",
                                "args": args, "repeats": repeats})
        return float(reply.get("seconds") or 0.0), bool(reply.get("ok"))


def observed_digest(reply):
    """One reply -> the digest to compare.

    A REFUSAL IS AN ANSWER and digests as one: how a subject rejects bad input is behaviour a
    reimplementation has to reproduce, so `{"ok": false}` is compared rather than discarded.
    """
    if reply.get("ok"):
        return digest(True, reply.get("value"), "")
    return digest(False, None, reply.get("error", ""))


def matching_reply(reference, actual, policy):
    if policy and reference.get('ok') and actual.get('ok'):
        return compare_numeric(reference.get('value'), actual.get('value'), policy)[0]
    return observed_digest(reference) == observed_digest(actual)


def score(passed, total, speedup, compliant=True):
    """The published formula. Correctness unlocks speed; nothing is capped."""
    if not compliant:
        return 0.0
    if total <= 0:
        return 0.0
    correctness = passed / total
    if passed < total:
        return 0.5 * correctness
    return 0.5 + 0.5 * speedup


def main():
    global PENDING_REPORT, ACTIVE_ROOT
    with ExitStack() as contexts:
        try:
            status = evaluate(contexts)
        finally:
            ROOTS.clear()
            ACTIVE_ROOT = None
    if PENDING_REPORT is not None:
        path, fields = PENDING_REPORT
        with open(path, 'w') as handle:
            json.dump(fields, handle, indent=2)
        print(json.dumps(fields, indent=2))
    return status

def evaluate(contexts):
    global ISOLATION_REPORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--workspace", default="/app")
    args = parser.parse_args()

    here = args.task_root
    reward_path = os.environ.get("REWARD_PATH", "/logs/verifier/reward_detail.json")
    os.makedirs(os.path.dirname(reward_path), exist_ok=True)
    if os.path.lexists(reward_path):
        os.unlink(reward_path)
    frozen = json.load(open(os.path.join(here, "expectations.json")))
    policy = frozen.get('numeric_policy')
    if policy or frozen.get('comparison') == 'envelope':
        validate_numeric_policy(policy)
    target = frozen.get('target_language')
    target_evidence = None
    target_replay = False

    def report(**fields):
        global PENDING_REPORT
        fields['isolation'] = ISOLATION_REPORT
        fields['verifier_sha256'] = VERIFIER_SHA256
        if target:
            fields['verification_scope'] = 'reference-self-replay' if target_replay else 'target-submission'
            fields['target_build'] = target_evidence
            if target_evidence is not None:
                execution = ROOTS['candidate'].execution_report()
                fields['target_execution'] = execution
                if execution['denied'] or not execution['complete']:
                    fields.update(reward=0.0, correct=False, timing_valid=False, speedup=0.0,
                                  note='target execution policy was violated or its evidence is incomplete')
                target_evidence['runtime_enforced'] = execution['enforced'] and execution['complete'] and not execution['denied']
                if fields.get('timing_valid'):
                    reference_execution = ROOTS['reference'].execution_report()
                    fields['reference_execution'] = reference_execution
                    if not reference_execution['complete']:
                        fields.update(reward=0.0, timing_valid=False, speedup=0.0,
                                      note='reference execution audit did not complete')
        PENDING_REPORT = (reward_path, fields)
        return 0 if fields.get("reward", 0) > 0 else 1

    # ABSOLUTE. `Popen` resolves argv[0] against the process's cwd, not against the `cwd` it is
    # given, so a relative path plus `cwd=` looks the path up twice and finds nothing -- reported as
    # "no such file" for a file that is plainly there.
    workspace = os.path.abspath(args.workspace)
    submission = [os.path.join(workspace, "run.sh")]
    reference = [os.path.join(os.path.abspath(here), "reference", "run.sh")]
    target_replay = target and workspace == os.path.dirname(reference[0])
    reference_workspace, reference_base, reference_environment = reference_context(here)
    if reference_base != '/':
        reference = [os.path.join(reference_workspace, 'run.sh')]
        if target_replay:
            workspace = reference_workspace
            submission = list(reference)
            args.workspace = workspace
    if not target and not os.path.exists(submission[0]):
        return report(reward=0.0, correct=False, correctness_passed=0,
                      correctness_total=sum(not item.get('dropped') for item in frozen['graded']), speedup=0.0,
                      note="no run.sh in the workspace, so there is nothing to grade")

    if target and not USE_CONFINEMENT:
        return report(reward=0.0, correct=False, correctness_passed=0,
                      correctness_total=sum(not item.get('dropped') for item in frozen['graded']),
                      speedup=0.0, note='target-language evaluation requires confinement')
    if USE_CONFINEMENT:
        ROOTS['candidate'] = contexts.enter_context(Root(workspace, uid=50001,
            runtime_base=reference_base if target_replay else '/',
            runtime_environment=reference_environment if target_replay else None))
        reference_dir = os.path.dirname(reference[0])
        ROOTS['reference'] = contexts.enter_context(Root(reference_dir, uid=50002,
            runtime_base=reference_base, runtime_environment=reference_environment))
        ISOLATION_REPORT = verify_pair(ROOTS['reference'], ROOTS['candidate'])
        if target and not target_replay:
            try:
                known = [Path(reference_dir) / name for name in ('program', 'serve.bin')]
                target_evidence = build_target(ROOTS['candidate'], target, reference_binaries=known)
                ROOTS['candidate'].audit_execution(target_evidence['entry_argv'] + frozen.get('target_helpers', []))
                ROOTS['reference'].audit_execution()
                submission = target_evidence['entry_argv']
            except Exception as error:
                return report(reward=0.0, correct=False, correctness_passed=0,
                              correctness_total=sum(not item.get('dropped') for item in frozen['graded']),
                              speedup=0.0, note='target-language build refused: ' + str(error)[:1500])
    # The task writer may have left a package reference tree in tests/reference. E7 must drive
    # exactly the emitted reference and report its real stderr; it must never silently substitute
    # the factory workspace.
    passed = 0
    total = sum(not item.get('dropped') for item in frozen['graded'])
    mismatches = []
    note = ""
    try:
        reference_values = {}
        if policy:
            with Subject(reference, cwd=os.path.dirname(reference[0]), role='reference') as baseline:
                for expectation in frozen['graded']:
                    if expectation.get('dropped'):
                        continue
                    pid = expectation['probe_id']
                    reply = baseline.call(frozen['probes'][pid])
                    if observed_digest(reply) != expectation['digest']:
                        return report(reward=0.0, correct=False, correctness_passed=0,
                                      correctness_total=total, speedup=0.0,
                                      note='reference no longer matches the frozen numerical baseline')
                    reference_values[pid] = reply
        with Subject(submission, cwd=workspace) as subject:
            for expectation in frozen["graded"]:
                if expectation.get("dropped"):
                    continue
                probe = frozen["probes"][expectation["probe_id"]]
                try:
                    reply = subject.call(probe)
                    same = (matching_reply(reference_values[expectation['probe_id']], reply, policy)
                            if policy else observed_digest(reply) == expectation['digest'])
                    if same:
                        passed += 1
                    else:
                        mismatches.append(expectation["probe_id"])
                except Exception as exc:
                    note = "the submission stopped answering: %s" % exc
                    break
    except Exception as exc:
        return report(reward=0.0, correct=False, correctness_passed=0, correctness_total=total,
                      speedup=0.0, note="the submission would not start: %s" % exc)

    if passed < total or total == 0:
        return report(reward=score(passed, total, 0.0), correct=False,
                      correctness_passed=passed, correctness_total=total, speedup=0.0,
                      note=note or "not every graded probe matched the reference: %s" %
                           ", ".join(mismatches[:8]))

    # TIMED ONLY ONCE CORRECT, and on inputs held out of grading, so that a submission cannot
    # answer them during the correctness pass and replay a cache when the clock starts.
    speedup, note = measure_speed(frozen, submission, reference, args, here)
    return report(reward=score(passed, total, speedup) if TIMING_REPORT.get("usable") else 0.0,
                  timing=TIMING_REPORT, timing_valid=bool(TIMING_REPORT.get("usable")), correct=True,
                  correctness_passed=passed, correctness_total=total,
                  speedup=round(speedup, 4), note=note)


def measure_speed(frozen, submission, reference, args, here):
    """-> (speedup, a note saying how it was arrived at).

    Paired and alternated: reference and submission are timed one after the other on the same
    input, so that a machine that slows down half way through slows both. A difference smaller than
    the reference's own run-to-run spread is reported as no change rather than as a small win.
    """
    global TIMING_REPORT
    TIMING_REPORT = {"usable": False, "protocol": PROTOCOL_VERSION}
    timed = frozen.get("timed") or []
    if not timed:
        return 0.0, "no workload was held out for timing, so speed was not measured"

    reference_dir = os.path.dirname(reference[0])
    if not os.path.exists(reference[0]):
        return 0.0, "the package ships no reference to time against"

    try:
        with Subject(submission, cwd=args.workspace) as mine, \\
             Subject(reference, cwd=reference_dir, role='reference') as ref:
            expected = {}
            policy = frozen.get('numeric_policy')
            for probe_id in timed:
                probe = frozen["probes"][probe_id]
                expected[probe_id] = ref.call(probe)
                frozen_digest = frozen.get('timed_expectations', {}).get(probe_id)
                if frozen_digest and observed_digest(expected[probe_id]) != frozen_digest:
                    return 0.0, 'timed reference no longer matches its frozen baseline'
                if not matching_reply(expected[probe_id], mine.call(probe), policy):
                    return 0.0, "held-out workload behavior differs from the reference"
            def cost(subject, probe_id):
                seconds = 0.0
                for _ in range(TIMED_REPEATS):
                    subject.activate()
                    started = time.perf_counter()
                    reply = subject.call(frozen["probes"][probe_id])
                    seconds += time.perf_counter() - started
                    if not matching_reply(expected[probe_id], reply, policy):
                        raise ValueError("timed response differs from reference")
                return seconds
            result = measure(lambda pid: cost(ref, pid), lambda pid: cost(mine, pid),
                             lambda shape, i: shape, timed)
            TIMING_REPORT = result.to_json()
            TIMING_REPORT["input_policy"] = "fixed held-out inputs; repeated calls in persistent process"
            TIMING_REPORT["cost_boundary"] = "evaluator wall clock; JSON transport included; comparison checks excluded"
            TIMING_REPORT["repeats"] = TIMED_REPEATS
            return result.speedup, result.note
    except Exception as exc:
        return 0.0, "timing could not be completed: %s" % exc


if __name__ == "__main__":
    sys.exit(main())
'''
