"""The serving half of the wire, one small template per language.

These are DATA, not framework code. The factory never imports a subject; it starts a process and
speaks JSON to it. What each of these does is adapt one language's calling convention to that wire,
and every one of them is small, because the wire was chosen to make them small.

That is the whole mechanism behind "any language". Adding the ninth language means adding a ninth
row to the table below and a ninth template beside it -- no branch anywhere in `core/`, no new
backend, no change to a freeze or a comparator. If a language ever needs more than a row, the wire
is wrong.

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

WHY A ROW CARRIES A BUILD AND NOT ONLY A COMMAND. Half of these languages are compiled, and a table
holding only "how to run it" can express none of them -- it can serve Python and Node and would have
quietly made "any language" mean "any interpreted language". The build step is part of what serving
a language IS, so it lives here beside the command rather than in whichever caller noticed first.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Shim:
    """How one language is served: what to write, what to compile, what to run.

    The three argv templates are formatted over `{entry}` (the shim file), `{subject}` (the
    subject's source beside it), `{binary}` (where a compiled result goes) and `{workdir}`.
    """

    template: str                  # the shim's own source file, shipped in the wheel
    subject: str                   # what the subject's source must be called beside it
    run: tuple                     # argv that starts the server
    build: tuple = ()              # argv lists run once, in the workspace, before serving
    tool: str = ""                 # the executable that must exist for any of this to work

    def commands(self, workdir: str, symbol: str = "entry") -> tuple:
        """-> (build argv lists, run argv), resolved against a real directory.

        `symbol` is WHICH function to serve, and it travels to the shim as an argument rather than
        being written into it. A shim that could only serve a function literally called `entry`
        could only serve a subject somebody had written for the occasion, and the material this
        factory sources is real code where the function is called `camel_to_snake`.
        """
        slots = {"entry": os.path.join(workdir, self.template),
                 "subject": os.path.join(workdir, self.subject),
                 "module": os.path.splitext(self.subject)[0],
                 "symbol": symbol,
                 "binary": os.path.join(workdir, "serve.bin"),
                 "workdir": workdir}
        build = tuple([part.format(**slots) for part in argv] for argv in self.build)
        return build, [part.format(**slots) for part in self.run]


# Language -> how it is served. One table so that "which languages can be a subject" is a question
# with a printable answer, rather than something a reader infers from a directory listing.
#
# The subject filename is per language and not a constant, which is easy to get wrong: a Go subject
# copied to `subject.py` compiles into nothing, and the failure arrives later as a link error about
# a missing symbol rather than as a statement about file naming.
TEMPLATES = {
    "python": Shim("serve.py", "subject.py", ("python3", "{entry}", "{module}", "{symbol}"),
                   tool="python3"),
    "javascript": Shim("serve.js", "subject.js", ("node", "--experimental-specifier-resolution=node", "{entry}", "subject.js", "{symbol}"), tool="node"),
    # TypeScript is compiled inside the sandbox with the toolchain declared by the image. This is
    # stable across Node versions and does not depend on experimental runtime flags.
    "typescript": Shim("serve.js", "subject.ts",
                       ("node", "--experimental-specifier-resolution=node", "{entry}", "compiled/subject.js", "{symbol}"),
                       build=(("tsc", "--target", "ES2022", "--module", "commonjs",
                               "--skipLibCheck", "--outDir", "{workdir}/compiled", "{subject}"),),
                       tool="tsc"),
    "ruby": Shim("serve.rb", "subject.rb", ("ruby", "{entry}"), tool="ruby"),
    "go": Shim("serve.go", "subject.go", ("{binary}",), tool="go",
               build=(("go", "build", "-o", "{binary}", "{entry}", "{subject}"),)),
    "rust": Shim("serve.rs", "subject.rs", ("{binary}",), tool="rustc",
                 # The subject is reached as `mod subject;` from the shim, so only the shim is named
                 # to the compiler and the subject is found beside it.
                 build=(("rustc", "-O", "--edition", "2021", "-o", "{binary}", "{entry}"),)),
    "c": Shim("serve.c", "subject.c", ("{binary}",), tool="cc",
              build=(("cc", "-std=c11", "-O2", "-o", "{binary}", "{entry}", "{subject}"),)),
    "cpp": Shim("serve.c", "subject.cpp", ("{binary}",), tool="c++",
                # The C shim serves C++ too: it speaks the wire and calls one extern "C" entry
                # point, which is a boundary C++ already has. A second near-identical template
                # would be a copy to keep in step for no behaviour of its own.
                build=(("c++", "-std=c++17", "-O2", "-x", "c", "-c", "{entry}", "-o",
                        "{workdir}/serve.o"),
                       ("c++", "-std=c++17", "-O2", "-o", "{binary}", "{workdir}/serve.o",
                        "{subject}"))),
    "java": Shim("Serve.java", "Subject.java", ("java", "-cp", "{workdir}", "Serve"), tool="javac",
                 build=(("javac", "-d", "{workdir}", "{entry}", "{subject}"),)),
}


def available() -> list[str]:
    """Which languages a subject can be written in, as far as this installation is concerned.

    Template present, which is a statement about the wheel. Whether the machine can actually run
    one is a different question with a different answer -- see `usable`.
    """
    return sorted(name for name, shim in TEMPLATES.items()
                  if os.path.exists(os.path.join(_HERE, shim.template)))


def usable(language: str) -> bool:
    """Whether this machine has the toolchain the language needs.

    Separate from `available` on purpose. A missing template means the factory cannot serve the
    language at all; a missing compiler means THIS host cannot, while the container the task ships
    with very well may. Collapsing them would make a laptop's contents look like a design limit.
    """
    shim = TEMPLATES.get((language or "").strip().lower())
    return bool(shim) and bool(shutil.which(shim.tool))


def load(language: str) -> Shim:
    """-> how to serve this language.

    Raises rather than falling back to a default. A missing shim means the language cannot be
    served, and quietly serving it as Python would produce a task that fails at freeze time with an
    error about syntax rather than about support.
    """
    key = (language or "").strip().lower()
    shim = TEMPLATES.get(key)
    if shim is None:
        raise LookupError(
            "no shim for %r; a subject on the call seam can be written in %s. Adding one means "
            "adding a template here -- nothing in core/ changes."
            % (language, ", ".join(available()) or "(none installed)"))
    if not os.path.exists(os.path.join(_HERE, shim.template)):
        raise LookupError("%r is listed but its template %s is missing" % (language, shim.template))
    return shim


def source(shim: Shim) -> str:
    return open(os.path.join(_HERE, shim.template), encoding="utf-8").read()


def materialise(workdir: str, language: str, subject_path: str,
                symbol: str = "entry") -> tuple:
    """Put a subject and its shim in one directory. -> (build argv lists, run argv).

    One implementation because there are two callers -- serving a subject and measuring its
    coverage -- and a second copy of "what the subject file is called" is exactly the kind of
    duplication that goes out of step and is then diagnosed as a compiler problem.
    """
    shim = load(language)
    os.makedirs(workdir, exist_ok=True)
    destination = os.path.join(workdir, shim.subject)
    # A source that is ALREADY where it needs to be is not an error. It happens whenever the file
    # is conventionally named -- `subject.py` served as python -- and most of all in the mutant
    # path, which writes a perturbed copy into a scratch directory under exactly this name and then
    # asks to have it materialised. `copyfile` raises SameFileError for that, which subclasses
    # OSError and so escapes the build-failure handling below it, taking E3 down with it.
    if not (os.path.exists(destination) and os.path.samefile(subject_path, destination)):
        shutil.copyfile(subject_path, destination)
    with open(os.path.join(workdir, shim.template), "w", encoding="utf-8") as handle:
        handle.write(source(shim))
    return shim.commands(workdir, symbol)
