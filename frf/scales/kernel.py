"""The kernel scale: a computational routine, which is a module with three additions.

    kernel = module + an array vocabulary + a float envelope + a pluggable cost

That is not a slogan; it is why this file is short. Everything a kernel task needs from the pipeline
is what a module task needs, so this subclasses `Module` and changes three things rather than
restating a scale.

    ARRAY VOCABULARY. Already in the schema: `float_array`, `int_array`, `complex_array`, with a
    dtype and a shape. A numeric routine cannot be expressed without them, and nothing else in the
    factory needs them -- which is the evidence that the call seam was always wide enough for this.

    FLOAT ENVELOPE. A numeric kernel that reorders a reduction produces a bitwise-different and
    entirely correct answer. Exact comparison would fail every correct rewrite. The reference's own
    error is the ruler, and a candidate is correct while it is no worse.

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

from ..core.scale import Candidate, Spec
from ..observe.probes.schema import ARRAY_KINDS
from .module import Module

# Shapes a numeric routine is drawn at. Larger than the module scale's, because a kernel's cost is
# supposed to be in the arithmetic: at sixteen elements the measurement is dominated by the call.
SHAPES = ({"n": 256}, {"n": 4096}, {"n": 65536})


class Kernel(Module):
    """A computational routine. A module with a numeric profile."""

    name = "kernel"

    def specify(self, candidate: Candidate) -> Spec:
        """The module specification, plus what makes this one numeric.

        `cost` travels in the environment so the timing layer can honour it without this scale
        reaching into timing -- the same reason every other cross-stage decision is data.
        """
        spec = super().specify(candidate)
        detail = candidate.detail or {}
        environment = dict(spec.environment)
        environment.update({
            "comparison": "envelope",
            "cost": detail.get("cost", "wall-clock"),
            "gpus": int(detail.get("gpus", 0)),
            "gpu_types": list(detail.get("gpu_types", ())),
        })
        return Spec(name=spec.name, scale=self.name, language=spec.language,
                    description=spec.description, build=spec.build, invoke=spec.invoke,
                    entry=spec.entry, target_language=spec.target_language,
                    environment=environment, notes=spec.notes)

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
        if not any(param.kind in ARRAY_KINDS for param in material.schema.params):
            raise ValueError(
                "%s takes no array parameter, so it is a module rather than a kernel. Kernel tasks "
                "are numeric routines; the array kinds are %s."
                % (candidate.identity, ", ".join(ARRAY_KINDS)))
        return material
