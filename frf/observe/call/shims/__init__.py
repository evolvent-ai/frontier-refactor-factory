"""The serving half of the wire, one small template per language.

These are DATA, not framework code. The factory never imports a subject; it starts a process and
speaks JSON to it. What each of these does is adapt one language's calling convention to that wire,
and every one of them is about thirty lines, because the wire was chosen to make them that small.

That is the whole mechanism behind "any language". Adding the ninth language means adding a ninth
template here and nothing else -- no branch anywhere in `core/`, no new backend, no change to a
freeze or a comparator. If a language ever needs more than a template, the wire is wrong.

A shim is handed the subject's entry point and does three things:

    read a line, decode it            -- one JSON object per line, on stdin
    call the entry point              -- catching whatever the language calls a failure
    write one reply line              -- including for the failure, which is an ANSWER

The failure path is why these cannot be one-liners. How a subject rejects bad input is part of the
behaviour a reimplementation has to reproduce, so an exception has to arrive at the factory as
`{"ok": false, "error": ...}` rather than as a dead process.

`time` is served on this side of the pipe on purpose. A compiled subject charged for process startup
and JSON transport would be timed on the harness rather than on itself, and the quick subjects this
pipeline mostly produces are exactly where that overhead would dominate.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# Language -> the template that serves it, and the command that runs the result. The command is a
# format string over `{entry}`, the file the shim is written to.
#
# Kept as one table so that "which languages can be a subject" is a question with a printable
# answer, rather than something a reader has to infer from the contents of a directory.
TEMPLATES = {
    "python": ("serve.py", ["python3", "{entry}"]),
    "go": ("serve.go", ["go", "run", "{entry}"]),
    "javascript": ("serve.js", ["node", "{entry}"]),
    "typescript": ("serve.js", ["node", "{entry}"]),
    "ruby": ("serve.rb", ["ruby", "{entry}"]),
}


def available() -> list[str]:
    """Which languages a subject can currently be written in."""
    return sorted(name for name, (template, _) in TEMPLATES.items()
                  if os.path.exists(os.path.join(_HERE, template)))


def load(language: str) -> tuple[str, list[str]]:
    """-> (the shim's source, the argv that runs it once written).

    Raises rather than falling back to a default. A missing shim means this language cannot be
    served, and quietly serving it as Python would produce a task that fails at freeze time with an
    error about syntax rather than about support.
    """
    key = language.strip().lower()
    if key not in TEMPLATES:
        raise LookupError(
            "no shim for %r; a subject on the call seam can be written in %s. Adding one means "
            "adding a template here -- nothing in core/ changes."
            % (language, ", ".join(available()) or "(none installed)"))
    template, argv = TEMPLATES[key]
    path = os.path.join(_HERE, template)
    if not os.path.exists(path):
        raise LookupError("%r is listed but its template %s is missing" % (language, template))
    return open(path, encoding="utf-8").read(), list(argv)
