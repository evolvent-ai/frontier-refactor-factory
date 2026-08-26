# frontier-refactor-factory

Turns real code into benchmark tasks that ask for it to be made **faster** — or reimplemented in
another language — **without changing what it does**.

Nobody writes down the right answer. The factory runs a real reference implementation, freezes only
what repeats, and stores digest-only expectations. A submission is judged by running it, not by
reading it.

## Status

The shared pipeline, E2B backend, JSON call seam, process seam, four scale adapters, Harbor output,
5-run freeze, adequacy and evidence gates are implemented. Module, Kernel and Package have produced
real E2B tasks. Repo sourcing/build/workload harvesting and native multi-language E2B support are
implemented; a Repo task is production-qualified only after its packaged Harbor verifier reproduces
the frozen reference completely.

## Four scales

| scale | subject | observation |
|---|---|---|
| `module` | one real function or symbol | JSON call seam |
| `kernel` | one array/numeric routine | JSON call seam with numeric envelope |
| `package` | a real library public surface | JSON call seam with operation dispatch |
| `repo` | a complete real repository/program | process exit/stdout/stderr/files |

Repo is one scale. Its source pool may internally distinguish transformer tools, medium tools and
large performance repositories, but emitted Repo tasks remain full repository-scale tasks. A Repo
task is never replaced by a toy program, a function task, or a task that only runs `--help`.

## Setup

Use Python 3.12+ and a virtual environment:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,sandbox]'
.venv/bin/pytest -q
.venv/bin/frf doctor
.venv/bin/frf scales
```

Copy the credential template. Never put real credentials in YAML, source, or git:

```bash
cp .env.example .env
```

Fill `.env` (it is gitignored):

```dotenv
LLM_BASE_URL=https://your-openai-compatible-gateway.example/v1
LLM_API_KEY=...
LLM_MODEL=...
E2B_API_KEY=...
GITHUB_TOKEN=...
# Optional rotation pool:
# GITHUB_TOKENS=token_a,token_b
```

Credentials are read from process environment first and `.env` second. They are passed to E2B as
environment variables, never as pushed files.

## Running a configured E2B roll

A YAML config controls infrastructure and jobs:

```yaml
output_dir: tasks/roll
freeze_runs: 5
max_concurrent: 10
llm_max_concurrent: 8
llm_calls_per_minute: 60
sandboxed: true
jobs:
  - scale: module
    form: inplace
    source_language: python
    budget: 30
    max_attempts: 300
  - scale: kernel
    form: inplace
    source_language: python
    budget: 30
  - scale: package
    form: inplace
    source_language: python
    budget: 30
  - scale: repo
    form: inplace
    source_language: rust
    budget: 30
```

Run it:

```bash
.venv/bin/frf run --config run.yaml --dry-run
.venv/bin/frf run --config run.yaml
.venv/bin/frf status path/to/checkpoint.jsonl
.venv/bin/frf run --config run.yaml --resume path/to/checkpoint.jsonl
.venv/bin/frf validate tasks/roll
```

Meaning of the important fields:

- `freeze_runs`: must remain `5` for production.
- `max_concurrent`: E2B/candidate worker limit; each candidate receives an isolated workspace.
- `FRF_E2B_MAX_ACTIVE`: maximum live remote E2B sandboxes (default `8`); workers above this
  account/resource limit wait rather than causing concurrent OOM kills.
- `e2b_max_active`: the same limit in YAML `RunConfig`; use this for reproducible production runs.
- `llm_max_concurrent` and `llm_calls_per_minute`: independent LLM rate limits.
- `sandboxed: true`: mandatory for production; local-process is development-only.
- `source_language`: source filter; it does not change the scale semantics.
- `target_language` plus `form: cross`: cross-language framing.
- `budget`: in configured `frf run` rolls, the number of fully verified tasks to emit.
- `max_attempts`: finite candidate-attempt ceiling for reaching that target; defaults to `10 x budget`.
  A roll that exhausts it reports `target_met: false` instead of running without bound.

## Pipeline and safety rules

Every scale uses:

```text
SOURCE → SPECIFY → E2B BUILD → PROBE → FREEZE(5) → ADEQUACY → EVIDENCE → EMIT → E7 REPLAY
```

- Module/Kernel/Package use the JSON call seam; Repo uses the four-channel process seam.
- Expectations contain digests, never plaintext answers.
- `tests/reference/` is verifier-private and `environment_mode = "separate"`.
- Network is disabled by default.
- `solution/` is intentionally not emitted; Harbor's optional solution configuration remains empty.
- Package generators are AST-validated on the host and executed only inside E2B.
- Package adapters are language-specific data/adapter modules; the main pipeline remains language-neutral.
- Repo reference programs must come from the pinned upstream checkout and its real build, never from a
  recipe-generated toy program.
- Repo workload priority is project benchmark/regression corpus, project testdata/fixtures, harvested
  test invocations, and only lastly generic entrypoint smoke probes.

## Layout

```text
frf/core/                 language-neutral pipeline, gates, scoring, Harbor, sandbox
frf/observe/call/         JSON call seam and language shim data
frf/observe/process/      four-channel process seam
frf/observe/probes/       deterministic schemas and E2B generator runner
frf/source/               enumerable registries, package adapters, repo survey/harvest
frf/scales/               kernel/module/package/repo adapters
DESIGN.md                 authoritative design record
```

## TODO

### Package

- [ ] Finish real E2B smoke evidence for every required package language, phased as:
  Python → JavaScript/TypeScript → Go/Rust → Ruby/Java/C/C++.
- [ ] Complete offline dependency-closure materialization for each ecosystem; reject packages that
  can only install from the network.
- [x] Make generator validation enforce operation coverage, valid/error/boundary balance and
  distinctness before freeze.
- [x] Gate Package emission on reference replay at `correctness_passed == correctness_total`.
- [ ] Produce and audit 10 high-quality Package tasks with 10 E2B workers.

### Repo

- [x] Complete RepoSurvey → build recipe → workload harvest as one automatic path.
- [ ] Support native multi-language repo layouts: Go/Cargo workspaces, multiple binaries, CMake
  targets, `package.json`/`bin`, and repository-specific build targets. This is required for the
  production corpus; it is phased by observed source supply, not optional scope.
- [x] Use project test scripts (`harvest_files`) and project corpus/fixtures (`harvest_corpus`).
- [ ] Connect project benchmark/regression workloads without inventing a new subject program.
- [ ] Produce and audit 10 real Repo performance tasks with 10 E2B workers. Keep FrontierSWE-scale
  repository difficulty; do not replace it with small CLI/function tasks.

### Scale

- [x] Make roll mode stop only after the requested number of emitted, fully verified tasks, or its
  explicit finite candidate-attempt ceiling is exhausted.
- [ ] Persist per-stage latency, E2B startup cost, source/API calls and refusal reasons.
- [x] Add crash-safe checkpoint/ledger semantics: retry factory failures, skip emitted/material.
- [ ] Verify checkpoint/resume under a multi-hour high-concurrency soak.
- [ ] After Package and Repo each reach 10, run the unified 40-task format/security/E7 audit.
- [x] Add bounded scalability validation for 32-worker scheduling, active-sandbox limits and resume.
- [ ] Run a bounded multi-hour production soak; do not require 1000 real tasks merely to prove
  scheduler scalability.

### Required language rollout

Multi-language support is a production requirement, not a Python-only fallback. Each language
enters the production pool only after its native E2B toolchain, offline dependency policy, source
adapter, task shape, Harbor verifier, and independent reference replay have all passed. A language
may be scheduled in a later phase, but it may not be silently converted to Python or counted as
supported before its phase gate passes.

`DESIGN.md` is authoritative. If implementation and design disagree, fix the implementation or update
the design before continuing.
