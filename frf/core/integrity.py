"""Stopping a submission from handing its work back to the thing it replaces.

The task is "make this faster with identical behaviour". The shortest way to identical behaviour is
to call the original, and a submission that does so is perfectly correct and has implemented
nothing. `evidence.cannot_delegate_to_the_reference` is the check that a task defends against this;
this module is the defence it checks for.

TWO MEASURES, BECAUSE THEY FAIL DIFFERENTLY AND NEITHER COVERS THE OTHER.

    SOURCE INSPECTION runs before anything is measured. Every import and every call in the
    submission is matched against what the task permits, and a hit scores zero outright. This is
    what catches `import reference` -- the one-line submission that passes every behavioural check
    there is.

    EXECUTION ISOLATION runs during timing. The two sides run under separate restricted accounts
    with a cap on processes, and whichever is not being timed is suspended. Without it a candidate
    can subcontract to a process the clock never sees, or make the reference look slow by competing
    with it for the machine.

NEITHER INVOLVES A JUDGEMENT. The rules come from the task, so the same submission gets the same
verdict every time. That is what makes this an audit rather than an opinion, and it is why the
inspection below is textual and syntactic rather than an attempt to decide what code MEANS.

WHY THIS IS NOT A SANDBOX, and the distinction matters. A determined adversary with arbitrary code
execution defeats source inspection -- `getattr(__import__("importlib"), ...)` is not going to be
caught by reading imports, and pretending otherwise would be the dangerous kind of false comfort.
What this stops is the submission that takes the obvious shortcut, which is the overwhelming
majority of them. What stops the rest is that the reference is not in the image at all: the
strongest form of this defence is absence, and everything here is the second line.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field

# Files worth reading when looking for a delegation. Anything else in a submission tree is data as
# far as this is concerned; a compiled blob cannot be inspected textually and the honest response to
# one is to say so rather than to scan it for strings and call that evidence.
SOURCE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".rs", ".c", ".h", ".cc", ".cpp",
                   ".hpp", ".java", ".rb")

# How a language says "bring in someone else's code". One pattern per family rather than a parser
# per language: this looks for a NAME being reached for, and every one of these forms puts that name
# in plain sight. A submission that assembles the name at run time is out of reach of any of them,
# which is stated in the module docstring rather than papered over here.
_REACHES = (
    re.compile(r"^\s*import\s+([\w.]+)", re.M),                        # python, java
    re.compile(r"^\s*from\s+([\w.]+)\s+import\b", re.M),               # python
    re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]""", re.M),        # node
    re.compile(r"""\bimport\s+.*?from\s*['"]([^'"]+)['"]""", re.M),    # es modules, typescript
    # A QUOTED IMPORT PATH, which is the only way Go can write one -- `import "reference"` and,
    # inside a grouped block, a bare `"reference/fast"` on its own line, optionally with a local
    # alias in front of it. None of the patterns above accept it: the first requires a word
    # character where Go puts a quote, and the fourth requires a `from`. So Go delegation was
    # invisible to the whole inspection while Go is a language this factory serves and sources for
    # -- E6's source half was a no-op for every Go task. Also catches a bare ES-module
    # side-effect import (`import 'reference';`), which had the same hole.
    re.compile(r"""^\s*(?:import\s+)?(?:[\w.]+\s+)?["`']([\w./-]+)["`']\s*$""", re.M),
    re.compile(r"""^\s*#\s*include\s*[<"]([^>"]+)[>"]""", re.M),       # c, c++
    re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)", re.M),                # rust
    re.compile(r"^\s*(?:extern\s+)?crate\s+(\w+)", re.M),              # rust
    re.compile(r"""\brequire(?:_relative)?\s+['"]([^'"]+)['"]""", re.M),   # ruby
)

# Forms whose whole purpose is to name something at run time, which is how a submission reaches for
# a forbidden module without ever writing its name where the patterns above would see it. Their
# presence is not proof of anything -- plenty of honest code uses them -- so they are reported
# separately from a hit, and it is the task's list that decides whether they are allowed at all.
_INDIRECTION = (
    re.compile(r"\b__import__\s*\(", re.M),
    re.compile(r"\bimportlib\s*\.\s*import_module\s*\(", re.M),
    re.compile(r"\beval\s*\(", re.M),
    re.compile(r"\bexec\s*\(", re.M),
    re.compile(r"\bsubprocess\b|\bos\.system\s*\(|\bos\.popen\s*\(", re.M),
    re.compile(r"\bchild_process\b", re.M),
    re.compile(r"\bRuntime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\b", re.M),
)


@dataclass(frozen=True)
class Finding:
    """One place a submission reached for something it must not."""

    path: str
    line: int
    name: str
    text: str

    def __str__(self) -> str:
        return "%s:%d reaches %r" % (self.path, self.line, self.name)


@dataclass
class Inspection:
    """What reading the submission found. Empty `hits` is the only passing result."""

    hits: list = field(default_factory=list)
    indirection: list = field(default_factory=list)
    files_read: int = 0
    files_skipped: int = 0

    @property
    def clean(self) -> bool:
        return not self.hits

    def names(self) -> list:
        return sorted({finding.name for finding in self.hits})

    def to_json(self) -> dict:
        return {"clean": self.clean,
                "hits": [str(h) for h in self.hits[:20]],
                "indirection": [str(h) for h in self.indirection[:20]],
                "files_read": self.files_read, "files_skipped": self.files_skipped}


def inspect(root: str, forbidden: tuple, *, allowed: tuple = ()) -> Inspection:
    """Read a submission tree and report every reach for something forbidden.

    `forbidden` is matched against a reached name and against its dotted prefixes, so forbidding
    `reference` also catches `reference.core.fast` -- the submodule is the same delegation wearing a
    longer name, and listing every submodule a package might grow is not a rule anyone can maintain.

    `allowed` wins over `forbidden` on an exact match. Some tasks legitimately permit one module
    from an otherwise banned package, and the alternative to expressing that here is expressing it
    in whichever caller remembered.
    """
    banned = {name.strip() for name in forbidden if name and name.strip()}
    permitted = {name.strip() for name in allowed if name and name.strip()}
    found = Inspection()
    if not banned:
        return found

    for path in _source_files(root):
        try:
            text = open(path, encoding="utf-8", errors="strict").read()
        except (OSError, UnicodeDecodeError):
            # Unreadable or not text. Counted rather than ignored: "we inspected 4 files and skipped
            # 900" is a very different claim from "we inspected the submission", and a caller that
            # cannot see the second number cannot tell them apart.
            found.files_skipped += 1
            continue
        found.files_read += 1
        relative = os.path.relpath(path, root)

        for pattern in _REACHES:
            for match in pattern.finditer(text):
                name = match.group(1)
                if name in permitted:
                    continue
                if _is_banned(name, banned):
                    found.hits.append(Finding(relative, _line_of(text, match.start()), name,
                                              match.group(0).strip()[:120]))
        for pattern in _INDIRECTION:
            for match in pattern.finditer(text):
                found.indirection.append(Finding(relative, _line_of(text, match.start()),
                                                 match.group(0).strip(),
                                                 match.group(0).strip()[:120]))
    return found


def _is_banned(name: str, banned: set) -> bool:
    """Whether a reached name is, or lives inside, something forbidden.

    Both separators, because the same question is asked of `a.b.c`, `a::b::c` and `a/b/c` depending
    on the language, and a rule that only understood dots would let a Rust or Node submission
    through by writing the same delegation with a different punctuation mark.
    """
    for separator in (".", "::", "/"):
        parts = name.split(separator)
        for index in range(1, len(parts) + 1):
            if separator.join(parts[:index]) in banned:
                return True
    return name in banned


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _source_files(root: str):
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", "target")]
        for name in files:
            if name.endswith(SOURCE_SUFFIXES):
                yield os.path.join(directory, name)


# ------------------------------------------------------------------------------------------------
# Execution isolation.
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Isolation:
    """How the two sides are kept apart while one of them is being timed.

    Reported rather than assumed. `evidence.cannot_delegate_to_the_reference` returns INCONCLUSIVE
    when isolation is not in force, and it can only do that if something tells it the truth --
    which is this type. A default of "yes, isolated" would turn the check into decoration.
    """

    enforced: bool
    accounts: bool = False           # reference and candidate run as different users
    process_cap: int = 0             # 0 means uncapped
    suspends_idle_side: bool = False  # the untimed side is stopped, not merely idle
    reason: str = ""

    def to_json(self) -> dict:
        return {"enforced": self.enforced, "accounts": self.accounts,
                "process_cap": self.process_cap,
                "suspends_idle_side": self.suspends_idle_side, "reason": self.reason}


# What a timed process may spawn. Enough for a runtime that uses a thread pool or a GC thread;
# far below what a submission would need to farm its work out to a fleet of helpers.
PROCESS_CAP = 64


def isolation_for(backend, *, applied: bool = False) -> Isolation:
    """What isolation is ACTUALLY IN FORCE for a subject served through this backend.

    `applied` is the whole of the answer, and it must be passed by whatever started the subject --
    it means "I wrapped the command with `restricted_argv` and I suspend the untimed side". A
    backend NAME is not evidence of any of that.

    THIS FUNCTION WAS WRONG IN EXACTLY THE WAY IT WARNS AGAINST. It used to return enforced=True
    for any backend called `docker` or `remote`, on the reasoning that a container is where
    isolation happens. But nothing wrapped anything: `restricted_argv` existed and was called from
    nowhere, nothing ever sent SIGSTOP, and the subject was started as the same user with no
    process cap. E6 then reported HOLDS -- "no forbidden import or call, and timing runs isolated"
    -- for a defence that had never once been applied. A check that certifies an absent measure is
    worse than no check, because it is indistinguishable from a real one in the provenance.

    So the name buys nothing. A container makes the defence POSSIBLE; only applying it makes the
    defence real, and only the code that applied it can say so.
    """
    name = getattr(backend, "name", "") or "none"
    if applied and name in ("docker", "remote"):
        return Isolation(enforced=True, accounts=True, process_cap=PROCESS_CAP,
                         suspends_idle_side=True,
                         reason="each side runs in its own container under an unprivileged account "
                                "with a process cap, and the untimed side is stopped while the "
                                "other is measured")
    if applied:
        # Wrapped, but on a backend that shares this machine. The account and the cap are real; the
        # separation is not, because both sides still see the same kernel and the same filesystem.
        return Isolation(enforced=False, accounts=True, process_cap=PROCESS_CAP,
                         reason="the command was restricted, but %r shares this machine with the "
                                "factory, so the two sides are not genuinely separated" % name)
    if name in ("docker", "remote"):
        return Isolation(enforced=False,
                         reason="%r could isolate the two sides, but nothing wrapped the subject "
                                "with an unprivileged account or a process cap, so no isolation is "
                                "actually in force" % name)
    return Isolation(enforced=False,
                     reason="%r shares this machine and this user with the factory, so work handed "
                            "to another process would be invisible to the clock" % name)


def restricted_argv(argv: list, *, user: str = "nobody", cap: int = PROCESS_CAP) -> list:
    """Wrap a command so it runs as an unprivileged user under a process cap.

    Used inside a container, where `nobody` exists and the wrapper binaries are present. It is
    deliberately a wrapper around the caller's argv rather than a change to how the subject is
    started: the subject must be started identically for the reference and for the candidate, or
    the comparison measures the wrapper.
    """
    limited = ["sh", "-c", "ulimit -u %d 2>/dev/null; exec \"$@\"" % cap, "--"] + list(argv)
    if shutil.which("setpriv"):
        return ["setpriv", "--reuid", user, "--regid", "nogroup", "--init-groups", "--"] + limited
    if shutil.which("su"):
        return ["su", "-s", "/bin/sh", user, "-c",
                " ".join(_shell_quote(part) for part in limited)]
    # Neither wrapper present. Returning the argv unchanged would silently drop the defence, so the
    # caller is made to deal with it -- `isolation_for` is what reports the consequence.
    raise LookupError(
        "no way to drop privileges here: neither setpriv nor su is present. Running the two sides "
        "as the same user would leave the delegation check unable to conclude anything.")


def _shell_quote(part: str) -> str:
    import shlex
    return shlex.quote(part)
