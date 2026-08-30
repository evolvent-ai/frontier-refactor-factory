# frontier-refactor-factory

Turns real code into benchmark tasks that ask for it to be made **faster** — or reimplemented in
another language — **without changing what it does**.

Nobody writes the answer down. The factory runs a real reference, freezes only what repeats across
five runs, and ships digest-only expectations. A submission is judged by running it.

`DESIGN.md` is authoritative. If the implementation disagrees with it, fix one of them.

## Four scales

| scale | subject | observed through |
|---|---|---|
| `kernel` | one numeric/array routine | JSON call seam, numeric envelope |
| `module` | one function | JSON call seam |
| `package` | a library's public surface | JSON call seam, operation dispatch |
| `repo` | a whole program | process: exit code, stdout, stderr, file tree |

Every scale runs the same pipeline:

```
SOURCE → SPECIFY → BUILD → PROBE → FREEZE(5) → ADEQUACY → EVIDENCE → EMIT → REPLAY
```

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,sandbox]'
.venv/bin/pytest -q
.venv/bin/frf doctor
```

Credentials come from the environment first, then `.env` (gitignored). Never in YAML or source.

```dotenv
LLM_BASE_URL=   LLM_API_KEY=   LLM_MODEL=
E2B_API_KEY=    GITHUB_TOKEN=          # GITHUB_TOKENS=a,b for a rotation pool
```

## Running

Copy `example-run.yaml` — every other `*.yaml` here is gitignored, because a run config is scratch.

```yaml
output_dir: /path/to/results
ledger_file: /path/to/results/run.ledger.jsonl
freeze_runs: 5              # 5 in production
e2b_max_active: 8           # live sandboxes
sandboxed: true             # local-process is development only
jobs:
  - scale: module
    form: inplace           # or: cross, with target_language
    source_language: python
    budget: 25              # tasks to EMIT
    max_attempts: 250       # candidate ceiling; exhausting it reports target_met: false
    max_per_repository: 2   # attempts per repo, per job
```

```bash
.venv/bin/frf run --config run.yaml --dry-run
.venv/bin/frf run --config run.yaml
.venv/bin/frf validate  <output_dir>
.venv/bin/python scripts/audit_matrix.py <output_dir>
```

**Two traps worth knowing before configuring a batch:**

- `max_per_repository` is counted **per job**. 25 tasks in one job holds the cap across all 25;
  five jobs of five reset it five times and may take the cap from the same repository each time.
- The cap counts **attempts**, not emitted tasks. At a 14% yield, 4 attempts contribute well under
  one task — tighten it for high-yield scales only.

## Status

Measured, not projected. `refused_factory: 0` on every batch below.

| scale | languages with an attested task | yield last measured |
|---|---|---|
| `kernel` | python, js, ts, go, rust, cpp | — |
| `module` | python, js, ts, go, rust, cpp | 3/3 |
| `package` | python, js, go | 2/3 |
| `repo` | go, rust, ruby | 2/14 |

18 of the 32 scale×language cells carry an attested task. Cells closed by an evidenced refusal
(real attempts, material fault) rather than by output: `repo/{python,javascript,typescript,cpp}`,
`module/ruby`, `kernel/java`.

Both task forms are proved end to end: same-language, and cross-language (python → javascript,
enforced by shipping an image with no python toolchain).

## Layout

```
frf/core/            language-neutral pipeline, gates, scoring, Harbor, sandbox
frf/observe/call/    JSON call seam: shims, bridges, package dispatch
frf/observe/process/ four-channel process seam
frf/source/          enumerable indexes, miners, package adapters
frf/scales/          kernel / module / package / repo
scripts/             audit_matrix.py and friends
```

Language-specific code lives in tables at the seam (`_GRAMMARS`, `TEMPLATES`, `_GENERATORS`,
`_RECONCILERS`, `_REGISTRY`); `core/pipeline.py` carries no language or scale branch.
`tests/test_layering.py` holds the boundaries around that — core never imports a seam, a seam never
imports a scale — but the absence of branches is a property nothing currently tests.

## TODO

### Blocking a cell

- [ ] **Ship JS/TS dependencies into the task.** `node_modules` is excluded in five places, so a
      repo-scale JS task builds with `npm install --offline` against dependencies that were never
      shipped and fails `ENOTCACHED`. Blocks `repo/javascript` and `repo/typescript`. Needs a size
      decision first: carrying the tree costs hundreds of MB per task.
- [ ] **Java subjects do not answer inside the freeze budget.** The miner and bridge are correct —
      candidates reach freeze — but the subject times out. Blocks all four java cells. Measure JVM
      start and per-probe cost in the sandbox before changing anything.
- [ ] **`kernel/ruby` and `package/ruby` source zero candidates.** Ruby is mineable (305 functions
      across the checkouts on hand); kernel additionally needs an array parameter, and package needs
      a gem whose closure is pure stdlib.
- [ ] `package/{typescript,rust,cpp}`: dispatchers exist and were never hit by usable material.

### Throughput

- [ ] `sourcing.walk` restarts paging at 0 on every call. The shared batch memory makes it correct,
      but it re-fetches early pages, and GitHub search allows 30 requests/minute.
- [ ] Waves are capped at `min(remaining_tasks, room)`, which is 1 when the budget is 1 — so a
      single-task roll is serial no matter how many workers are configured.
- [ ] Persist per-stage latency, sandbox startup cost and source API calls alongside the ledger.

### Corpus quality

- [ ] Domain spread has no measure. Topic chains are algorithm-heavy, so a large roll can be many
      tasks of one shape while every task is individually attested. `max_per_repository` bounds how
      much comes from one project; nothing bounds how much comes from one kind of code.
- [ ] Verify checkpoint/resume under a multi-hour, high-concurrency soak.

### Done recently

- [x] Cross-language wired end to end: config → `Spec` → instruction, with a refusal when a scale
      would drop the target language rather than a silent same-language emit.
- [x] Every unbounded wait bounded: sandbox lifetime > freeze budget, freeze budget applied on the
      batched path, per-chunk and whole-call deadlines on `call_many`, and a bounded wait around
      the E2B event stream, which does not end on its own.
- [x] A freeze timeout reports `too-slow-to-freeze` rather than `will-not-repeat-itself`.
- [x] Gateway 402/403/429/5xx retried with backoff; deterministic 4xx still fails at once.
