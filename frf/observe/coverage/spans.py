"""Turning what a coverage tool reports into the one shape `Reach` is expressed in.

Every backend in this package ends up doing the same three things, and only the middle one differs:
run the subject under instrumentation, read what the tool wrote, and turn that into "these lines of
these files ran, out of these". The reading differs per tool -- V8 reports byte ranges, gcov reports
lines, Go reports statement blocks with line and column -- and the last step does not differ at all,
so it lives here.

WHY BYTE OFFSETS NEED CARE. V8 answers in offsets into the file, and converting one to a line number
by counting newlines is O(file) per offset, which on a large subject with thousands of ranges is
quadratic and has genuinely taken minutes. `line_index` builds the boundaries once and bisects.

WHAT COUNTS AS AN EXECUTABLE LINE when the tool does not say. Some tools report only what ran, so
the denominator has to come from somewhere, and "every line in the file" is wrong -- it counts
blanks, comments and closing braces, and a well-commented subject would report low coverage for
being well documented. `executable_lines` is a deliberately conservative approximation for the
brace-and-hash family of languages: it is used only where the tool gives us nothing better.
"""
from __future__ import annotations

import bisect

from ...core.adequacy import Reach

# How many unreached regions to name. Enough to aim a repair at, few enough that a provenance file
# stays readable -- the fraction is the summary and this is the actionable part.
DARK_LIMIT = 20

# Line prefixes that cannot be executed in any of the C-like languages here. Not a parser: it is a
# fallback denominator for tools that report only positive hits, and over-counting a line as
# executable understates reach, which is the safe direction for a number used to REJECT a corpus.
_NOT_CODE_PREFIXES = ("//", "/*", "*", "*/", "#", "--")
_NOT_CODE_EXACT = ("{", "}", "};", ")", ");", "],", "end", "})", "});", "else", "} else {")


def line_index(text: str) -> list:
    """Start offsets of every line, for turning a byte offset into a line number by bisection."""
    starts, position = [0], text.find("\n")
    while position != -1:
        starts.append(position + 1)
        position = text.find("\n", position + 1)
    return starts


def line_of(starts: list, offset: int) -> int:
    """Which 1-based line an offset falls on."""
    return bisect.bisect_right(starts, offset)


def executable_lines(text: str) -> set:
    """A conservative guess at which lines could execute, for tools that report only hits.

    Used as a denominator only where the instrumentation gives none. Where the tool reports both
    executed and unexecuted lines -- gcov and Go both do -- that is used instead and this is not
    consulted, because a real denominator always beats a heuristic one.
    """
    lines = set()
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped in _NOT_CODE_EXACT:
            continue
        if stripped.startswith(_NOT_CODE_PREFIXES):
            continue
        lines.add(number)
    return lines


def assemble(backend: str, per_file: dict) -> Reach:
    """Per-file (executed lines, executable lines) -> one Reach.

    `per_file` maps a display name to a pair of sets. Both are sets rather than counts because the
    executed set has to be intersected with the executable one: instrumentation regularly reports a
    hit on a line the denominator does not contain -- a closing brace that carries a return, a
    declaration the compiler moved -- and counting those would produce reach above 100%, which
    makes the number untrustworthy in the direction nobody checks.
    """
    total = reached = 0
    dark = []
    for name, (executed, executable) in sorted(per_file.items()):
        if not executable:
            continue
        hit = executed & executable
        total += len(executable)
        reached += len(hit)
        missed = len(executable) - len(hit)
        if missed:
            dark.append((missed, name))

    ranked = tuple("%s (%d line(s) unreached)" % (name, missed)
                   for missed, name in sorted(dark, reverse=True)[:DARK_LIMIT])
    return Reach(reached=reached, total=total, dark=ranked, backend=backend)


def unmeasured(backend: str) -> Reach:
    """No backend, or the measurement did not happen. NOT a measured zero.

    The two mean opposite things and the distinction decides whether a task ships: `Reach.measured`
    is False here, so adequacy reports an absence and the floor carries the verdict alone. A
    measured zero means the instrumentation attached and found nothing executed, which is a broken
    tracer and correctly fails. Returning zero for "I could not run" would refuse tasks for our bug.
    """
    return Reach(backend=backend)
