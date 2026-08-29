"""The call seam: a subject served as a function behind a JSON wire.

WHAT IS HERE. One question, asked in one place: can this seam actually serve a function the miner found
in somebody else's repository? It takes three parts to answer yes, and the third has two shapes, which
is why the answer is computed rather than listed.

    a miner   -- something has to FIND the function. `source/function_miner.supported`.
    a shim    -- something has to speak the wire in that language. `shims.TEMPLATES`.
    a BINDING -- something has to connect the shim to a function it has never heard of.

The third part is the one that gets forgotten, because the other two are so visible. It arrives two
ways, and both are real:

    THE SHIM BINDS ITSELF, when the runtime can look a name up. serve.py does
    `getattr(import_module(mod), symbol)`, serve.js indexes `subject[symbol]`, serve.rb does
    `send(ENTRY, *args)`. Handed any function under its own name, they serve it. `Shim.binds_symbol`.

    A BRIDGE IS GENERATED, when the compiler needs the types written out. serve.go requires
    `func Entry(args []interface{}) (interface{}, error)` in `package main`, and the material is
    `func Knapsack(maxWeight int, weights, values []int) int` in `package dynamic`. So a file is
    generated per candidate declaring what the shim expects and calling the real symbol.
    `bridge.supported`.

WHY THIS IS A PREDICATE AND NOT A LIST. It was a list, briefly, and the list was wrong in both
directions. go/rust/java/cpp were declared servable on the strength of a miner and a shim, and every
candidate of the first Go kernel batch died at build. Then ruby was declared unservable because
serve.rb hard-coded the name `entry` -- true of the file, not of the language, and fixed with one argv
slot. A predicate over the parts cannot drift from the parts. `core/capabilities.py` states its own
judgement about EVIDENCE, and `tests/test_capabilities.py` holds that judgement against this answer
about MECHANISM.
"""
from __future__ import annotations


def servable(language: str) -> bool:
    """Whether a mined function in `language` can be found, served, and bound to its shim.

    Says nothing about whether such a task has been PRODUCED. That is evidence, and it lives in the
    capability registry and the audit records; this is only what the code is able to do.
    """
    from . import bridge, shims
    from ...source.function_miner import supported as minable

    key = (language or "").strip().lower()
    shim = shims.TEMPLATES.get(key)
    if shim is None or not minable(key):
        return False
    return bool(shim.binds_symbol) or bridge.supported(key)
