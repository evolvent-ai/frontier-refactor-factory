# frontier-refactor-factory

Turns real code into benchmark tasks that ask for it to be made **faster** — or reimplemented in
another language — **without changing what it does**.

Nobody writes down what the right answer is. The factory measures the reference implementation and
freezes what it reproducibly does; that recording is the expectation. A submission is judged by
running it, never by reading it, so it may be written in any language and in any style.

```python
from frf import Factory
from frf.scales import Module

factory = Factory().register(Module())
result = factory.build("module", budget=20)

print(result.summary())
# {'scale': 'module', 'attempted': 20, 'emitted': 3, 'yield_rate': 0.15,
#  'refused_material': 17, 'refused_factory': 0, 'trustworthy': True, ...}
```

> **Status: the spine is built, the scales are not.** The pipeline, both observation seams, the
> evidence battery, scoring, timing and the emitter are implemented and tested. `frf/scales/` is
> still empty, so the snippet above does not run yet — `Factory().register(...)` with your own scale
> does. See *Extending it* below, and `DESIGN.md` section 16 for what "module is done" will mean.

## Four scales, one pipeline

| scale | what gets refactored | language |
|---|---|---|
| `kernel` | one computational routine | original |
| `module` | one function or symbol | original |
| `package` | a package's whole public surface | original or any |
| `repo` | an entire repository | original or any |

They differ in exactly two places — **where the material comes from**, and **where the subject is
observed**. Everything else is one implementation shared by all four.

Observation has two forms, and the split is not a detail:

- **A repository is watched as a process**: exit code, stdout, stderr, and the files it leaves
  behind. Every program in every language has all four, which is why nothing in the framework
  branches on language.
- **The three smaller scales are called over a JSON wire**, one object per line. A subject in a new
  language needs a ~30-line shim and nothing else.

Trying to force these into one shape is the mistake this design exists to avoid: judging a function
through a process means judging the command-line wrapper you had to write for it, and judging a
repository through a call means writing a per-language parser to decide what "the entry point" is.

## Scoring

```
score = 0.5 + 0.5 × speedup      if correctness == 1.0
score = 0.5 × correctness        otherwise
```

Correctness is an **unlock**, not a weight: short of complete, no amount of speed helps. Partial
correctness is still reported as a gradient, because missing three cases of 1567 and missing half of
them call for opposite next steps.

Compliance is audited independently and short-circuits both — a submission that delegated its work
back to the reference scores zero, not partial credit for the parts it did honestly.

A speedup inside the machine's own noise arrives at scoring as exactly `1.0`. The noise floor is
calibrated **on the evaluating machine, during the run**, by timing the reference against itself;
there is no constant anywhere, because a number measured on the authoring host describes a machine
nobody is graded on.

## Three orthogonal rulers

The most common way to ship a worthless task is to confuse these.

| | asks | audience |
|---|---|---|
| **Verifier** | is this submission correct? | the solver |
| **Evidence** | is the verifier honest? | us |
| **Adequacy** | did we measure the right thing? | the task |

A verifier that returns zero for everything passes every check of the form *a bad submission must
score zero*. So the evidence battery bites from both ends: the reference must score full marks (E1)
**and** nothing trivial may (E2).

And a corpus containing only `git --version` passes the **entire** battery — the reference
reproduces it, an empty submission scores zero, every channel bites — while testing nothing. Green
evidence does not mean the task is worth anything; that is what adequacy is for.

The battery is E1–E8: ceiling, floor, every channel bites, no runnable reference shipped, points are
about the subject, no delegation, the emitted package reproduces itself, and seed independence.
A scale may skip a check only by showing its failure is **structurally impossible** — "unlikely
here" is not a reason.

## Extending it

A scale answers four questions and implements none of the eight stages:

```python
class MyScale:
    name = "my-scale"

    def find(self, budget):        ...   # candidates, from an enumerable index
    def specify(self, candidate):  ...   # what to build, how to call it
    def observe(self):             ...   # which seam
    def probes(self, spec):        ...   # where inputs come from

Factory().register(MyScale()).build("my-scale", budget=10)
```

**Adding a scale must not require editing anything in `frf/core/`.** That is the load-bearing claim
of the design, and it is a test rather than a promise — `tests/test_factory_interface.py` defines a
scale inside the test file and ships a task with it.

## Setup

```bash
cp .env.example .env      # gateway URL and key; .env is gitignored
pip install -e .
pytest -q
```

Credentials are read by exactly one function, environment first and `.env` second. They reach a
sandbox as environment variables and never as a pushed file — a file lands on a disk this process
does not own and can come home inside a pulled artefact.

## Layout

```
frf/
  core/        knows nothing about what an observation looks like
    pipeline.py    the eight stages, and the gates between them
    scale.py       what a scale must answer
    evidence.py    the battery that checks the verifier
    scoring.py     correctness unlocks speed
    timing.py      paired, interleaved, noise calibrated in place
    harbor.py      the shipped package format
    sandbox.py     where builds and freezes run
  observe/     knows exactly what an observation looks like
    process/       seam A: four channels, unstable lines masked by position
    call/          seam B: JSON wire, unstable probes discarded whole
    probes/        schema sampling and container-run generators
    compare/       exact, structural, and numeric-envelope comparison
  scales/      one file per scale: the four answers, nothing else
```

The boundary is drawn by a single question — *does this code know the shape of an observation?* —
rather than by what sounds general. Freezing N runs and keeping what agrees sounds universal, but
every line of it names either lines-with-positions or a returned value, and those are different
things. So it lives in `observe/`, twice, and that is not duplication: it is one idea landing on two
data shapes.

## Design

`DESIGN.md` is the reference, and it is the authority: where an implementation disagrees with it,
the implementation is wrong. It records the decisions **and the retractions** — four claims from
earlier drafts were removed after they failed scrutiny, and they are documented so nobody proposes
them again.
