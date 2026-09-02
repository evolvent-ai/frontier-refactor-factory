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
    # Where a GENERATED BRIDGE is written, for a shim that cannot bind a mined symbol itself. Empty
    # for the dynamic shims, which need none.
    #
    # A SEPARATE FILE OR THE SUBJECT ITSELF, and the compiler decides which. Go names every file on
    # the command line, so `bridge.go` is its own translation unit -- and must be named in the build
    # argv as `{bridge}`, or it is written and never compiled. Rust names only the shim: `serve.rs`
    # reaches the subject as `mod subject;`, so a third file would be invisible to rustc. Setting this
    # equal to `subject` means the bridge is APPENDED to the subject, which is also why it needs no
    # argv slot -- and why, being one module, it can call the mined function by its plain name.
    bridge: str = ""

    @property
    def bridge_is_subject(self) -> bool:
        """Whether the bridge is appended to the subject rather than written beside it."""
        return bool(self.bridge) and self.bridge == self.subject
    # WHETHER THIS SHIM CAN BIND A MINED FUNCTION BY ITSELF. The third thing a call seam needs, and
    # the one that is easy to miss because two of them are so visible.
    #
    # A dynamic runtime closes the last gap at run time: `serve.py` does
    # `getattr(import_module(mod), symbol)` and then `_entry(*args)`, `serve.js` does
    # `subject[symbol](...args)`, `serve.rb` splats `entry(*args)`. Handed any function the miner
    # found, under its own name, with its own arity, they serve it.
    #
    # A static shim cannot. `serve.go` requires `func Entry(args []interface{}) (interface{}, error)`
    # in `package main`; `serve.rs` requires `pub fn entry(args: &crate::Json) -> Result<Json,String>`
    # in `mod subject`; Serve.java reflects for `Subject.entry(List)`. Real mined material looks
    # nothing like that -- `func CoinChange(coins []int, amount int) int` in `package dynamic` -- so
    # serving it needs a per-candidate GENERATED BRIDGE that declares the expected package and entry,
    # unpacks the JSON arguments into concrete types, and calls the real symbol.
    #
    # Recorded here because it is a property of the template, and because leaving it implicit already
    # cost a batch: with a miner and a shim both present, go/rust/java/cpp were declared call-capable,
    # and every candidate died at build with `found packages main (serve.go) and dynamic (subject.go)`
    # -- and, once the package name was fixed by hand, `undefined: Entry` underneath it.
    binds_symbol: bool = False

    def commands(self, workdir: str, symbol: str = "entry", *, bridged: bool = False) -> tuple:
        """-> (build argv lists, run argv), resolved against a real directory.

        `symbol` is WHICH function to serve, and it travels to the shim as an argument rather than
        being written into it. A shim that could only serve a function literally called `entry`
        could only serve a subject somebody had written for the occasion, and the material this
        factory sources is real code where the function is called `camel_to_snake`.

        `bridged` says whether a generated bridge was written beside the subject. TOLD, not probed:
        this once tested `os.path.exists`, which is wrong for the caller that emits a task package --
        it resolves `workdir="."` so the run.sh it writes is portable, and the check then asked about
        the factory's own working directory rather than the room the commands will run in.
        """
        slots = {"entry": os.path.join(workdir, self.template),
                 "subject": os.path.join(workdir, self.subject),
                 "module": os.path.splitext(self.subject)[0],
                 "symbol": symbol,
                 "binary": os.path.join(workdir, "serve.bin"),
                 # Present even when this shim needs no bridge, so that `format` cannot raise
                 # KeyError on a template that mentions it. A shim with no bridge never names it.
                 "bridge": os.path.join(workdir, self.bridge or "bridge.unused"),
                 "workdir": workdir}
        # A BRIDGE IS COMPILED ONLY IF THERE IS ONE. The build argv names `{bridge}` because a mined
        # symbol needs it, but a subject that already declares the shim's entry point itself -- a
        # hand-written one in a test, or any subject written for this wire -- is materialised without
        # a binding and so has no bridge file. Naming a file that was never written fails the build
        # with a message about a missing path, which says nothing about either the subject or the
        # bridge. Written, it is compiled; not written, it is dropped from the command.
        # ONLY A SEPARATE BRIDGE CAN BE ABSENT. When the bridge IS the subject -- rust, java, cpp, where
        # the generated entry is appended to the mined file -- there is nothing to drop, and dropping
        # it removed the SUBJECT from the build: `undefined reference to entry_error` for an
        # unbridged C++ subject that declares its own entry. A subject is always compiled.
        bridge_path = slots["bridge"] if not self.bridge_is_subject else ""
        build = []
        for argv in self.build:
            resolved = [part.format(**slots) for part in argv]
            build.append([part for part in resolved
                          if not bridge_path or part != bridge_path or bridged])
        return tuple(build), [part.format(**slots) for part in self.run]


# Language -> how it is served. One table so that "which languages can be a subject" is a question
# with a printable answer, rather than something a reader infers from a directory listing.
#
# The subject filename is per language and not a constant, which is easy to get wrong: a Go subject
# copied to `subject.py` compiles into nothing, and the failure arrives later as a link error about
# a missing symbol rather than as a statement about file naming.
TEMPLATES = {
    "python": Shim("serve.py", "subject.py", ("python3", "{entry}", "{module}", "{symbol}"),
                   tool="python3", binds_symbol=True),
    # THE SHIM IS `.cjs`, THE SUBJECT IS NOT. A package that declares `"type": "module"` makes
    # every neighbouring `.js` file ESM, and this shim is CommonJS -- it died with
    # `ReferenceError: require is not defined` on eleven of thirteen javascript package candidates.
    # `.cjs` is exactly the escape hatch: CommonJS whatever the package says.
    #
    # The SUBJECT keeps `.js`, because it is not ours. A mined function is usually ESM
    # (`export function ...`), and renaming it to `.cjs` makes Node parse it as CommonJS and fail
    # with `SyntaxError: Unexpected token 'export'` -- which is what happened when this fix was
    # first written for both files at once. Node 22 lets a `.cjs` file `require()` an ESM module, so
    # the shim can load either kind and neither has to pretend to be the other.
    "javascript": Shim("serve.cjs", "subject.js",
                       ("node", "--experimental-specifier-resolution=node", "{entry}",
                        "subject.js", "{symbol}"), tool="node", binds_symbol=True),
    # TypeScript is compiled inside the sandbox with the toolchain declared by the image. This is
    # stable across Node versions and does not depend on experimental runtime flags.
    #
    # COMPILED IN PLACE, not into a compiled/ subdirectory. A package subject's dispatcher imports
    # its module tree with RELATIVE paths ("./tech-interview-handbook/..."), and those are resolved
    # from the compiled file's directory. Compiling into a subdirectory moved the root away from the
    # module tree, so every package candidate died in E3 with ERR_MODULE_NOT_FOUND -- the import
    # resolved against compiled/ instead of the workspace root where the tree actually lives.
    # Same split as javascript: the shim is `.cjs`, and `tsc`'s output keeps the name it emits.
    "typescript": Shim("serve.cjs", "subject.ts",
                       ("node", "--experimental-specifier-resolution=node", "{entry}",
                        "subject.js", "{symbol}"),
                       build=(("tsc", "--target", "ES2022", "--module", "commonjs",
                               "--skipLibCheck", "--outDir", "{workdir}", "{subject}"),),
                       tool="tsc", binds_symbol=True),
    # RUBY NEEDED NO BRIDGE, ONLY THE SYMBOL. It was briefly recorded as unable to bind, which was
    # true of the file and not of the language: the splat was always general, but the NAME was
    # hard-coded to `entry` and no `{symbol}` was passed, so a mined `two_sum` raised NameError. A
    # dynamic runtime can look a method up by name -- `send` -- so the fix is one argv slot and a
    # `send`, not a generated bridge with concrete types. That is what separates it from go/rust/
    # java/cpp, whose compilers need the types written out.
    "ruby": Shim("serve.rb", "subject.rb", ("ruby", "{entry}", "{symbol}"), tool="ruby",
                 binds_symbol=True),
    # THREE FILES, not two: the shim, the mined subject, and the generated bridge that declares the
    # `Entry` serve.go calls and converts JSON arguments into the subject's own types. Naming
    # `{bridge}` in the build argv is what makes it compile; writing it and forgetting that is a file
    # on disk the linker never sees, which fails as `undefined: Entry`.
    # GOTOOLCHAIN=local BECAUSE THE SANDBOX HAS NO NETWORK AND SAYING SO IS CHEAPER THAN FINDING OUT.
    # A modern go.mod may carry `toolchain go1.27.0`, and Go's default is to go and FETCH that
    # toolchain -- which `GOPROXY=off` then blocks, after the attempt. Two candidates in a real batch
    # refused as `go: download go1.27.0 for linux/amd64: toolchain not available`, which reads like a
    # missing dependency and is really a repository asking for a compiler this image does not carry.
    # `local` makes Go use what is installed and say so immediately, so the refusal is fast and names
    # the real reason.
    #
    # THE IMAGE IS NOT BUMPED TO CHASE THIS. The repositories that demanded a newer toolchain were
    # applications rather than libraries, and an offline sandbox refuses them at their dependency
    # closure anyway -- so a newer compiler would move the refusal without preventing it. The pin in
    # `core/shims/dockerfiles.py` is one line and worth revisiting from a host that can verify a tag
    # exists; guessing one from here would break every Go build instead of two.
    "go": Shim("serve.go", "subject.go", ("{binary}",), tool="go", bridge="bridge.go",
               build=(("env", "GOPROXY=off", "GOSUMDB=off", "GOTOOLCHAIN=local",
                       "go", "build", "-o", "{binary}", "{entry}", "{bridge}", "{subject}"),)),
    # THE BRIDGE IS THE SUBJECT FILE, because rustc is handed only the shim and reaches the subject as
    # `mod subject;` -- a third file would be invisible to the compiler. So the generated `entry` is
    # APPENDED to the mined source, which also puts the mined function in scope under its plain name.
    "rust": Shim("serve.rs", "subject.rs", ("{binary}",), tool="rustc", bridge="subject.rs",
                 # The subject is reached as `mod subject;` from the shim, so only the shim is named
                 # to the compiler and the subject is found beside it.
                 build=(("rustc", "-O", "--edition", "2021", "-o", "{binary}", "{entry}"),)),
    "c": Shim("serve.c", "subject.c", ("{binary}",), tool="cc",
              build=(("cc", "-std=c11", "-O2", "-o", "{binary}", "{entry}", "{subject}"),)),
    # THE BRIDGE IS THE SUBJECT FILE here too, and for a third distinct reason: serve.c is compiled AS
    # C, so it cannot hold C++ at all, and its JSON parser is `static` -- unreachable from another
    # translation unit. The bridge therefore carries its own reader and is appended to subject.cpp,
    # where the mined function is already declared above it and needs no synthesised prototype.
    "cpp": Shim("serve.c", "subject.cpp", ("{binary}",), tool="c++", bridge="subject.cpp",
                # The C shim serves C++ too: it speaks the wire and calls one extern "C" entry
                # point, which is a boundary C++ already has. A second near-identical template
                # would be a copy to keep in step for no behaviour of its own.
                build=(("c++", "-std=c++17", "-O2", "-x", "c", "-c", "{entry}", "-o",
                        "{workdir}/serve.o"),
                       ("c++", "-std=c++17", "-O2", "-o", "{binary}", "{workdir}/serve.o",
                        "{subject}"))),
    # THE BRIDGE IS THE SUBJECT FILE, because Serve.java reflects for `Class.forName("Subject")` -- so
    # the generated class has to BE `Subject`, and a Java file may hold several top-level classes as
    # long as the public one matches the filename. `Subject.java` therefore holds the mined class with
    # its `public` stripped (see `bridge._reconcile_java`) and `public class Subject` beside it.
    "java": Shim("Serve.java", "Subject.java", ("java", "-cp", "{workdir}", "Serve"), tool="javac",
                 bridge="Subject.java",
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
                symbol: str = "entry", *, binding: dict | None = None) -> tuple:
    """Put a subject and its shim in one directory. -> (build argv lists, run argv).

    One implementation because there are two callers -- serving a subject and measuring its
    coverage -- and a second copy of "what the subject file is called" is exactly the kind of
    duplication that goes out of step and is then diagnosed as a compiler problem.

    `binding` is what a STATIC shim needs in order to reach a mined symbol at all: the parameter
    schema, what the function returns, and the package its file declared. Given one, a bridge is
    generated beside the subject and the subject is reconciled so the two can compile together.
    Omitted -- and it always is for python/javascript/typescript, whose shims bind a symbol
    themselves -- nothing extra is written and the behaviour is exactly as before.
    """
    shim = load(language)
    os.makedirs(workdir, exist_ok=True)
    bridged = bool(binding and shim.bridge)
    destination = os.path.join(workdir, shim.subject)
    generated = ""
    if bridged:
        # Imported here rather than at module scope: `bridge` is a sibling in this package and only
        # this branch needs it, so a shim table with no static language stays importable on its own.
        from .. import bridge as bridges
        generated = bridges.source(language, symbol=symbol,
                                   params=binding.get("params") or (),
                                   result=binding.get("result") or {},
                                   package=str(binding.get("package") or ""),
                                   owner=str(binding.get("owner") or ""))

    if shim.bridge:
        # A STATIC SUBJECT IS RECONCILED, NOT JUST COPIED. Go requires every file in a directory to
        # declare one package, and mined material declares its own -- `package sort` beside the
        # shim's `package main` fails the build with `found packages main (serve.go) and sort
        # (subject.go)`, which is what refused every candidate of the first Go kernel batch. A repo
        # that ships a program also has its own `func main`, which collides with the shim's.
        #
        # Read-then-write rather than copyfile, and it is deliberately safe for the mutant path,
        # which can materialise a file that is ALREADY at the destination: the text is fully read
        # before anything is written, and both `reconcile` and `attach` are idempotent.
        from .. import bridge as bridges
        with open(subject_path, encoding="utf-8", errors="surrogateescape") as handle:
            text = bridges.reconcile(language, handle.read())
        # SUBJECT AND BRIDGE IN ONE FILE, when the compiler is handed only the shim. rustc reaches the
        # subject as `mod subject;`, so a separate bridge.rs would never be compiled -- writing it
        # first and the subject second, as this function used to, silently overwrote it. Appended
        # here, in the one place that knows both texts.
        if bridged and shim.bridge_is_subject:
            text = bridges.attach(text, generated)
        with open(destination, "w", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(text)
        if bridged and not shim.bridge_is_subject:
            with open(os.path.join(workdir, shim.bridge), "w", encoding="utf-8") as handle:
                handle.write(generated)
    # A source that is ALREADY where it needs to be is not an error. It happens whenever the file
    # is conventionally named -- `subject.py` served as python -- and most of all in the mutant
    # path, which writes a perturbed copy into a scratch directory under exactly this name and then
    # asks to have it materialised. `copyfile` raises SameFileError for that, which subclasses
    # OSError and so escapes the build-failure handling below it, taking E3 down with it.
    elif not (os.path.exists(destination) and os.path.samefile(subject_path, destination)):
        shutil.copyfile(subject_path, destination)
    with open(os.path.join(workdir, shim.template), "w", encoding="utf-8") as handle:
        handle.write(source(shim))
    return shim.commands(workdir, symbol, bridged=bridged)
