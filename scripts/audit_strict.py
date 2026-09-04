#!/usr/bin/env python3
"""Strict quality audit for frontier-refactor-factory output.

THREE GATES, not two. The loose audit checked only `form` (something observable was graded) and
`attestation` (the 8-check battery held). Both can be satisfied by a task that measures nothing:

  * A repo task whose every graded step is an ERROR PATH -- the program refusing an invocation it
    does not accept, identically every time. Reproducing a refusal is perfectly reproducible, so it
    passes form and passes the battery. `semtools-faster` is 5 real steps out of 26.
  * A call-seam task with exactly TWO distinct digests across every kept probe. Two answers is the
    minimum that is not a constant, and a submission that returns either one has a coin-flip chance
    of being graded correct on any given probe. `ggnn-pytorch-faster`, `vec-km-faster` and four
    others are exactly this.

So the third gate is DISCRIMINATION: does the corpus actually constrain an implementation?

  * repo: over half of the graded steps must be real work (stdout with content, or exit 0), not
    error paths.
  * call seam: at least THREE distinct digests among kept probes, and over half the probes must
    have answered rather than failed.

No language cap is applied. The user's directive is explicit that language balance is not required
(two languages for package is fine), and an earlier cap of six-per-language silently turned repo's
29 passing tasks into "24, short 1" and sent this factory chasing a task it did not need.
"""
import hashlib
import json
import os
import sys
import tomllib
from collections import Counter, defaultdict

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/data/evolvent/shijian-workdir/ffr-results"
TARGET = 25

_H = lambda s: "sha256:" + hashlib.sha256(s.encode()).hexdigest()
ZERO = _H("0")          # the digest of exit code "0"
EMPTY = _H("")          # the digest of no output at all


def newest_per_task(root):
    """Every task, deduplicated by (scale, name, language), newest path winning.

    The same task name exists in several batch directories -- a showcase copy and the gap-fill run
    that fixed it -- and reading the stale one reports a defect that was already repaired.
    """
    best = {}
    for dirpath, _dirs, files in os.walk(root):
        if "task.toml" not in files:
            continue
        path = os.path.join(dirpath, "task.toml")
        try:
            data = tomllib.loads(open(path, "rb").read().decode())
        except Exception:                                  # noqa: BLE001 -- a broken file is not a task
            continue
        meta = data.get("metadata") or {}
        scale = meta.get("scale")
        language = meta.get("source_language") or "?"
        name = (data.get("task") or {}).get("name") or ""
        if scale not in ("kernel", "module", "package", "repo") or not name:
            continue
        key = (scale, name, language)
        stamp = os.path.getmtime(path)
        if key not in best or stamp > best[key][0]:
            best[key] = (stamp, dirpath, data)
    return best


def repo_steps(expectations, timed):
    """-> (real, error, total) graded steps. Timed scenarios are excluded; they grade duration."""
    real = error = total = 0
    for probe, steps in expectations.items():
        if probe in timed or not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            out = step.get("stdout") if isinstance(step.get("stdout"), dict) else {}
            code = step.get("exit_code") if isinstance(step.get("exit_code"), dict) else {}
            if not (out.get("graded") or code.get("graded")):
                continue
            total += 1
            produced = (out.get("graded") and out.get("line_count", 0) > 0
                        and out.get("digest") != EMPTY)
            succeeded = code.get("graded") and code.get("digest") == ZERO
            if produced or succeeded:
                real += 1
            else:
                error += 1
    return real, error, total


def call_probes(expectations):
    """-> (distinct digests, answered, kept) over probes that were not dropped or timed."""
    graded = expectations.get("graded")
    if not isinstance(graded, list) or not graded:
        return 0, 0, 0
    timed = set(expectations.get("timed") or [])
    kept = [g for g in graded
            if isinstance(g, dict) and not g.get("dropped") and g.get("probe_id") not in timed]
    digests = {g.get("digest") for g in kept}
    digests.discard(None)
    # `ok` is absent on older corpora; a probe that carries a digest answered with something.
    answered = sum(1 for g in kept if g.get("ok") is True or (g.get("ok") is None and g.get("digest")))
    return len(digests), answered, len(kept)


def judge(scale, root, meta, expectations):
    """-> (attested, form, strict, detail). `strict` is the discrimination gate."""
    held, total_checks = meta.get("evidence_checks_held"), meta.get("evidence_checks_total")
    attested = (isinstance(held, int) and isinstance(total_checks, int)
                and total_checks > 0 and held == total_checks)
    if scale == "repo":
        timed = set()
        timed_path = os.path.join(root, "tests", "timed.json")
        if os.path.exists(timed_path):
            try:
                loaded = json.load(open(timed_path))
                timed = set(loaded) if isinstance(loaded, list) else set()
            except Exception:                              # noqa: BLE001
                pass
        real, error, total = repo_steps(expectations, timed)
        form = real > 0
        strict = real > 0 and total > 0 and real / total >= 0.5
        return attested, form, strict, "real=%d/%d err=%d" % (real, total, error)
    distinct, answered, kept = call_probes(expectations)
    form = distinct >= 2
    strict = distinct >= 3 and kept > 0 and answered / kept >= 0.5
    return attested, form, strict, "digests=%d ok=%d/%d" % (distinct, answered, kept)


def main():
    rows = defaultdict(list)
    for (scale, name, language), (_stamp, root, data) in newest_per_task(ROOT).items():
        path = os.path.join(root, "tests", "expectations.json")
        if not os.path.exists(path):
            continue
        try:
            expectations = json.load(open(path))
        except Exception:                                  # noqa: BLE001
            continue
        if not isinstance(expectations, dict):
            continue
        attested, form, strict, detail = judge(scale, root, data.get("metadata") or {}, expectations)
        rows[scale].append((name, language, attested, form, strict, detail))

    print("%-8s %12s %8s  vs%d(strict)" % ("scale", "BOTH(loose)", "STRICT", TARGET))
    shortfall = {}
    for scale in ("kernel", "module", "package", "repo"):
        entries = rows[scale]
        loose = [e for e in entries if e[2] and e[3]]
        strict = [e for e in entries if e[2] and e[4]]
        languages = dict(Counter(e[1] for e in strict))
        verdict = "MET" if len(strict) >= TARGET else "SHORT %d" % (TARGET - len(strict))
        if len(strict) < TARGET:
            shortfall[scale] = TARGET - len(strict)
        print("%-8s %12d %8d  %-8s %s" % (scale, len(loose), len(strict), verdict, languages))

    print("\n=== passes loose, fails strict (weak discrimination) ===")
    for scale in ("kernel", "module", "package", "repo"):
        weak = [e for e in rows[scale] if e[2] and e[3] and not e[4]]
        if weak:
            print("\n%s (%d):" % (scale, len(weak)))
            for name, language, _a, _f, _s, detail in sorted(weak):
                print("  %-11s %-42s %s" % (language, name, detail))

    return 1 if shortfall else 0


if __name__ == "__main__":
    raise SystemExit(main())
