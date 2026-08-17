"""The wire a subject is called over, when the subject is a function rather than a program.

One JSON object per line, over stdin and stdout. That is the entire interface, and deliberately
the smallest thing that can express "call this with these arguments and tell me what came back":

    ->  {"id": 3, "op": "run",  "call": "distance", "args": [[1, 2], [3, 4]]}
    <-  {"id": 3, "ok": true,  "value": 2.8284271247461903}
    <-  {"id": 3, "ok": false, "error": "expected two points"}

`args` IS THE ARGUMENT LIST, NOT ONE PACKED ARGUMENT. In the example above `distance` is called
with TWO arguments, each a point -- not with one argument that happens to be a list of two points.
Every shim spreads it into the subject's own parameters, which is what lets a real function be
served unedited: material sourced from a registry is called `camel_to_snake(s)` and takes a string,
and a convention that handed it `["fooBar"]` would require editing somebody else's code before
grading it -- and then the task would grade the edit.

The distinction is easy to get wrong in a way that stays green: a subject written to unpack `args`
itself works perfectly against a shim that packs, so the two halves can disagree for as long as
nobody serves a function they did not also write. Nine shims agreeing is not evidence; the
`test_any_language` subjects are written to this contract in every language, which is.

WHERE THE LANGUAGE WILL NOT ALLOW IT. Python, JavaScript, TypeScript and Ruby spread `args` into the
subject's own parameters, so a function found in a real package is served exactly as its author
wrote it. Go, Rust, C, C++ and Java have no way to apply a runtime-length argument list to a fixed
signature without reflection this deliberately avoids, so their subjects receive the whole list and
index it. That is a limit of those languages rather than a second convention, and it costs nothing
where it applies: a subject in one of them is written for the task in any case, because the module
scale sources functions from registries that publish readable source, and those are the interpreted
ones.

WHY A WIRE AND NOT AN IMPORT. The candidate may be written in any language. If the factory imported
it, the factory would have to know how to import that language, and "supports any language" would
quietly become "supports the two we wrote loaders for". A subprocess speaking JSON over a pipe is
the one calling convention every language already has.

WHY `ok: false` IS AN ANSWER AND NOT A CRASH. How a subject REFUSES is part of its behaviour: a
reimplementation that gets every valid input right and every rejection wrong is not correct. So a
raised exception is captured and compared like any other outcome, rather than aborting the probe.

TWO OPERATIONS, AND THE SECOND EXISTS FOR HONESTY ABOUT TIME. `run` answers with a value. `time`
answers with seconds for N internal calls, self-measured on the far side of the pipe -- because a
compiled subject charged for process startup and JSON transport would be timed on this module rather
than on itself, and the quick subjects this pipeline mostly produces are exactly where that
overwhelms the measurement.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class Request:
    """One call to make."""

    id: int
    call: str                          # which entry point; the only entry for a single function
    args: list
    op: str = "run"                    # "run" | "time"
    repeats: int = 1                   # for op="time": how many internal calls to measure

    def encode(self) -> str:
        payload: dict[str, Any] = {"id": self.id, "op": self.op, "call": self.call,
                                   "args": self.args}
        if self.op == "time":
            payload["repeats"] = self.repeats
        return json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"


@dataclass(frozen=True)
class Response:
    """What came back. Exactly one of `value` / `error` / `seconds` is meaningful, per `op`."""

    id: int
    ok: bool
    value: Any = None
    error: str = ""
    seconds: float = 0.0

    @classmethod
    def decode(cls, line: str) -> "Response":
        """Parse one reply line.

        A malformed line is a FAILED CALL, not an exception out of the parser. The far side is a
        program someone else wrote: it can print a warning to stdout, die halfway through a line, or
        emit nothing at all, and every one of those is something the candidate did -- so it belongs
        in the graded record rather than crashing the harness that is grading it.
        """
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            return cls(id=-1, ok=False, error="unparseable reply: %r" % line[:200])
        if not isinstance(data, dict):
            return cls(id=-1, ok=False, error="reply was not an object: %r" % line[:200])
        return cls(id=int(data.get("id", -1)),
                   ok=bool(data.get("ok", False)),
                   value=data.get("value"),
                   error=str(data.get("error", "")),
                   seconds=float(data.get("seconds", 0.0) or 0.0))
