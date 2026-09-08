"""The kernel scale: a computational routine, which is a module with three additions.

    kernel = module + an array vocabulary + a float envelope + a pluggable cost

That is not a slogan; it is why this file is short. Everything a kernel task needs from the pipeline
is what a module task needs, so this subclasses `Module` and changes three things rather than
restating a scale.

    ARRAY VOCABULARY. Already in the schema: `float_array`, `int_array`, `complex_array`, with a
    dtype and a shape. A numeric routine cannot be expressed without them, and nothing else in the
    factory needs them -- which is the evidence that the call seam was always wide enough for this.

    FLOAT ENVELOPE. Floating results follow an explicit absolute/relative tolerance against the
    frozen original behavior. Upstream assertions take priority over declared-precision defaults.
    This does not claim an independently measured high-precision error bound for the reference.

    PLUGGABLE COST. Wall-clock is the default and the noisiest option. A routine in a closed
    simulator reports cycles, which are exact; a CPU kernel can report instructions retired. The
    timing layer takes callables that return a cost, so this is a choice rather than a rewrite.

GPU IS AN INTERFACE HERE, NOT AN IMPLEMENTATION. `gpus` and `gpu_types` are fields the harness
already reads, and a kernel task declares them. Nothing in this factory schedules a GPU or times a
CUDA event yet, and pretending otherwise would produce tasks nobody can run. CPU kernels are a real
and sufficient family on their own: vectorising, changing an algorithm, improving a memory layout
and removing temporaries are all measurable without a card.
"""
from __future__ import annotations

from ..core.scale import Candidate, Spec, TaskForm
from ..observe.probes.schema import ARRAY_KINDS
from .module import Module

# Shapes a numeric routine is drawn at. Larger than the module scale's, because a kernel's cost is
# supposed to be in the arithmetic: at sixteen elements the measurement is dominated by the call.
# Keep the largest frozen JSON/result artifact bounded. Larger numerical workloads belong in the
# held-out timing pass, not in every expectation payload; 65k-element vectors made one task's
# expectations tens of megabytes and amplified E2B transfer/memory cost across a batch.
SHAPES = ({"n": 256}, {"n": 4096}, {"n": 16384})


class Kernel(Module):
    """A computational routine. A module with a numeric profile."""

    name = "kernel"

    def find(self, budget: int):
        """Source numeric routines before spending E2B time on module-shaped functions."""
        wanted = max(budget * 8, budget)
        for candidate in super().find(wanted):
            params = ((candidate.detail or {}).get("schema") or {}).get("params", ())
            if any(str(param.get("kind")) in ARRAY_KINDS
                   for param in params if isinstance(param, dict)):
                yield candidate
                budget -= 1
                if budget <= 0:
                    return

    def specify(self, candidate: Candidate, *,
                task_form: TaskForm = TaskForm.INPLACE) -> Spec:
        """The module specification, plus what makes this one numeric.

        `cost` travels in the environment so the timing layer can honour it without this scale
        reaching into timing -- the same reason every other cross-stage decision is data.

        REBUILDING A SPEC MEANS RESTATING EVERY FIELD. This method constructs a second `Spec` to
        add the timing environment, and a field left out of that call is silently dropped however
        carefully the parent filled it in -- which is what happened to `task_form`.
        """
        spec = super().specify(candidate, task_form=task_form)
        detail = candidate.detail or {}
        environment = dict(spec.environment)
        from ..observe.compare.numeric_policy import select_numeric_policy
        environment.update({
            "comparison": "envelope",
            "numeric_policy": select_numeric_policy(self._material),
            "cost": detail.get("cost", "wall-clock"),
            "gpus": int(detail.get("gpus", 0)),
            "gpu_types": list(detail.get("gpu_types", ())),
            "scope_guidance": ("Focus on the selected numeric computation and entry symbol. Keep "
                               "argument types, numeric edge cases, precision/error behavior, "
                               "and allocation behavior unchanged."),
            "invocation_label": "Factory dispatch marker",
        })
        enriched = Spec(name=spec.name, scale=self.name, language=spec.language,
                    description=spec.description, build=spec.build, invoke=spec.invoke,
                    entry=spec.entry, target_language=spec.target_language,
                    task_form=spec.task_form,
                    environment=environment, notes=spec.notes)
        self._spec = enriched
        return enriched

    def probes(self, spec: Spec):
        """Sampled like a module's, at sizes where the arithmetic dominates the call."""
        source = super().probes(spec)
        source.shapes = SHAPES
        return source

    def _locate(self, candidate: Candidate):
        """Refuse material that is not numeric, at the point where it can still be refused cheaply.

        A subject whose parameters hold no array is a module task wearing the wrong label. Shipping
        it as a kernel would put a task in the set that the set's own description does not fit, and
        every number computed per scale afterwards would be measuring a mixture.
        """
        material = super()._locate(candidate)
        numeric_array = any(param.kind in ARRAY_KINDS
                            for param in material.schema.params)
        if not numeric_array:
            raise ValueError(
                "%s is not a numeric kernel: kernel tasks require an int_array, float_array "
                "or complex_array input" % candidate.identity)
        return material
