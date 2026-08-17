"""Line coverage for a Ruby subject, through the interpreter's own `Coverage` module.

Standard library, so no gem to install in an offline container. `Coverage.start(lines: true)` has to
be called before the subject is loaded, which is the whole reason this backend replaces the shim's
launch with a small wrapper rather than setting an environment variable: there is no variable that
arms it.

WHAT RUBY REPORTS, and it is the best-shaped answer of any tool here. One array per file, one entry
per line, where `nil` means the line is not executable at all, `0` means executable and never run,
and a positive number is a hit count. So the denominator is exact -- no guessing which lines are
code, no heuristic about comments or `end` keywords -- and the arithmetic is a filter over one list.

WHAT IS EXCLUDED. Everything outside the subject's own file. `Coverage` instruments the standard
library too, and a report that included it would carry a denominator of tens of thousands of lines
that no corpus of the subject could ever move.
"""
from __future__ import annotations

import json
import os

from . import driver, spans

# The wrapper that arms coverage, loads the shim, and writes the result out at exit. Written to the
# workspace rather than passed with `-e`, so that an error in it names a file and a line.
WRAPPER = '''\
require 'coverage'
require 'json'

# Armed BEFORE the shim is loaded. Ruby records a line only from the moment coverage starts, so
# arming it afterwards would report the subject's own body as never executed.
Coverage.start(lines: true)

at_exit do
  File.write(ARGV[0], JSON.generate(Coverage.result))
end

load File.join(File.dirname(__FILE__), 'serve.rb')
'''

REPORT = "coverage.json"


class RubyCoverage:
    """The `Coverage` module, driven through a wrapper that arms it first."""

    name = "ruby-coverage"
    language = "ruby"

    def measure(self, spec, probes) -> spans.Reach:
        with driver.Run(spec, probes, language=self.language,
                        build_override=_wrap) as run:
            if not run.ok:
                return spans.unmeasured(self.name)
            return self._read(run)

    def _read(self, run) -> spans.Reach:
        path = run.path(REPORT)
        if not os.path.exists(path):
            return spans.unmeasured(self.name)
        try:
            payload = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return spans.unmeasured(self.name)

        wanted = run.subject_name()
        for name, entry in payload.items():
            if os.path.basename(name) != wanted:
                continue
            lines = entry.get("lines") if isinstance(entry, dict) else entry
            executed, executable = set(), set()
            for index, count in enumerate(lines or (), 1):
                if count is None:
                    continue                       # not executable: a comment, a blank, an `end`
                executable.add(index)
                if count > 0:
                    executed.add(index)
            if executable:
                return spans.assemble(self.name, {wanted: (executed, executable)})

        # The run happened but the subject is not in the report, which means coverage was armed too
        # late or the file was loaded under another path. Unmeasured, never a zero.
        return spans.unmeasured(self.name)


def _wrap(workdir: str, build: list, argv: list):
    """Launch the wrapper instead of the shim; it loads the shim itself."""
    wrapper = os.path.join(workdir, "measure_coverage.rb")
    with open(wrapper, "w", encoding="utf-8") as handle:
        handle.write(WRAPPER)
    return build, ["ruby", wrapper, os.path.join(workdir, REPORT)]
