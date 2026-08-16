"""Declaring what a function accepts, and drawing inputs from it.

A single function has fixed parameter types, so what it accepts can be DECLARED and then sampled --
which is why this exists at all, and why a package's contract surface cannot use it. A package
exposes dozens of entry points whose valid inputs have nothing in common; expressing that here would
mean reinventing a type language badly. Those use a generator instead.

The vocabulary is deliberately small. Every kind here earns its place by being something a real
subject actually takes, and each one that is missing shows up as a subject the factory cannot serve
rather than as a wrong answer -- a failure mode worth preferring.

WHY DTYPE AND SHAPE ARE FIRST-CLASS. They are what makes a numerical routine expressible, and that
is the whole of the difference between the module scale and the kernel scale. A float array declared
without its dtype gets drawn as integers, which silently truncates the distribution and leaves every
float-specific path in the subject unexercised -- the corpus then grades a program the solver never
runs.

SEEDED, ALWAYS. A corpus that differs between two runs of the factory cannot be frozen, and an
Expectation is only worth something if the probes that produced it can be produced again.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

# What a parameter can be. Adding a kind is a deliberate act: it widens what the factory can serve,
# and every kind must be drawable deterministically from a seed and expressible as JSON, because it
# has to cross the wire in `protocol`.
SCALAR_KINDS = ("int", "float", "bool", "string", "bytes")
ARRAY_KINDS = ("int_array", "float_array", "complex_array")
COMPOUND_KINDS = ("list", "map")
KINDS = SCALAR_KINDS + ARRAY_KINDS + COMPOUND_KINDS

# Default numeric spread. Wide enough that a subject's branches on sign and magnitude are reached,
# small enough that a quadratic subject does not turn one probe into a benchmark of the harness.
_DEFAULT_LOW, _DEFAULT_HIGH = -1000.0, 1000.0
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-.,/"


class SchemaError(ValueError):
    """A schema that cannot be sampled. Raised at load time, not at draw time.

    Loudly and early: a schema with a typo produces a corpus of the wrong shape, and the freeze
    afterwards would happily record it. The cheapest moment to notice is before anything is built.
    """


@dataclass(frozen=True)
class Param:
    """One parameter of the subject under test."""

    kind: str
    dtype: str = ""                    # for arrays: float64 / int64 / complex128
    size: Any = None                   # int, or the name of a shape whose value is substituted
    low: float = _DEFAULT_LOW
    high: float = _DEFAULT_HIGH
    element: "Param | None" = None     # for list/map: what one element looks like
    sorted_: bool = False              # some subjects require monotonic input to mean anything

    @classmethod
    def from_json(cls, data: dict) -> "Param":
        kind = str(data.get("kind", ""))
        if kind not in KINDS:
            raise SchemaError("unknown kind %r; known kinds are %s" % (kind, ", ".join(KINDS)))
        element = data.get("element")
        if kind in COMPOUND_KINDS and element is None:
            raise SchemaError("kind %r must declare `element`: what one entry looks like" % kind)
        # DTYPE IS COERCED, NOT DEFAULTED. A float array whose dtype was left blank would be drawn
        # from the integer path, and the subject's float branches would never run.
        dtype = str(data.get("dtype", ""))
        if kind == "float_array" and dtype in ("", "int64"):
            dtype = "float64"
        if kind == "complex_array" and not dtype.startswith("complex"):
            dtype = "complex128"
        if kind == "int_array" and not dtype:
            dtype = "int64"
        return cls(kind=kind, dtype=dtype, size=data.get("size"),
                   low=float(data.get("low", _DEFAULT_LOW)),
                   high=float(data.get("high", _DEFAULT_HIGH)),
                   element=cls.from_json(element) if element else None,
                   sorted_=bool(data.get("sorted", False)))

    def to_json(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "low": self.low, "high": self.high}
        if self.dtype:
            out["dtype"] = self.dtype
        if self.size is not None:
            out["size"] = self.size
        if self.element is not None:
            out["element"] = self.element.to_json()
        if self.sorted_:
            out["sorted"] = True
        return out


@dataclass(frozen=True)
class Schema:
    """The full call signature: what to pass, in order."""

    params: list[Param] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "Schema":
        raw = data.get("params")
        if not isinstance(raw, list) or not raw:
            raise SchemaError("a schema needs a non-empty `params` list")
        return cls(params=[Param.from_json(p) for p in raw])

    def to_json(self) -> dict:
        return {"params": [p.to_json() for p in self.params]}


def _size_of(param: Param, shape: dict, default: int = 16) -> int:
    """Resolve a declared size against the shape being drawn for.

    A size may be a number or the NAME of a shape dimension. Naming it is what lets one schema be
    sampled at several sizes, which is how the timing pass can refuse a candidate that only got fast
    at one convenient size.
    """
    if isinstance(param.size, int):
        return max(0, param.size)
    if isinstance(param.size, str):
        return max(0, int(shape.get(param.size, default)))
    return default


def draw(param: Param, rng: random.Random, shape: dict) -> Any:
    """One parameter -> one JSON-encodable value."""
    if param.kind == "int":
        return rng.randint(int(param.low), int(param.high))
    if param.kind == "float":
        return rng.uniform(param.low, param.high)
    if param.kind == "bool":
        return rng.random() < 0.5
    if param.kind == "string":
        return "".join(rng.choice(_ALPHABET) for _ in range(_size_of(param, shape, 12)))
    if param.kind == "bytes":
        # Bytes cross the wire as a list of small integers: JSON has no byte string, and inventing
        # an encoding here would be one more thing every language on the far side has to agree with.
        return [rng.randint(0, 255) for _ in range(_size_of(param, shape, 12))]

    if param.kind in ARRAY_KINDS:
        n = _size_of(param, shape)
        if param.kind == "int_array":
            values: list[Any] = [rng.randint(int(param.low), int(param.high)) for _ in range(n)]
        elif param.kind == "float_array":
            values = [rng.uniform(param.low, param.high) for _ in range(n)]
        else:
            # A complex array draws BOTH components. Dropping the imaginary part would collapse a
            # complex subject onto its real path, so any branch that tests for complex input would
            # never be taken and the corpus would grade half the program.
            values = [[rng.uniform(param.low, param.high), rng.uniform(param.low, param.high)]
                      for _ in range(n)]
        if param.sorted_:
            values.sort(key=lambda v: v[0] if isinstance(v, list) else v)
        return values

    if param.kind == "list":
        n = _size_of(param, shape, 8)
        return [draw(param.element, rng, shape) for _ in range(n)]
    if param.kind == "map":
        n = _size_of(param, shape, 8)
        return {"k%d" % i: draw(param.element, rng, shape) for i in range(n)}
    raise SchemaError("no way to draw kind %r" % param.kind)


def sample(schema: Schema, seed: int, shape: dict | None = None) -> list:
    """A schema and a seed -> one argument list, reproducibly.

    The same seed and shape always give the same arguments. That is what makes an Expectation worth
    freezing: the probe it came from can be produced again, on another machine, months later.
    """
    rng = random.Random(seed)
    shape = shape or {}
    return [draw(p, rng, shape) for p in schema.params]
