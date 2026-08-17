"""The claim this whole design rests on, checked by running it in every language available.

"Any language" is the load-bearing promise. Everything else -- the wire instead of an import, the
shim table as data, the seam that never learns what it is talking to -- exists to make it true, and
none of it is worth anything if it has only ever been exercised in the language the factory happens
to be written in.

So this file takes ONE subject, expressed once per language, and puts every one of them through the
same seam: the same freeze, the same comparison, the same grading. The assertions are that they all
answer the same, and that nothing in the pipeline had to be told which was which.

WHY THE SUBJECTS ARE WRITTEN OUT RATHER THAN GENERATED. A generator would have to know each
language's syntax, which is the very knowledge this design claims not to need -- and putting it in
the test would prove the opposite of the point. These are what a real subject looks like: someone
else's code, in their language, that the factory never reads.

A LANGUAGE WHOSE TOOLCHAIN IS ABSENT IS SKIPPED, NOT FAILED. `shims.usable` is the difference
between "this factory cannot serve Go" and "this machine has no Go", and conflating them would make
a laptop's contents look like a design limit. What is NOT tolerated is skipping everything: the
final test fails if only Python ran, because a suite that silently degrades to one language would
report success for exactly the situation this file exists to detect.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                            # noqa: E402

from frf.observe.call import shims                                       # noqa: E402
from frf.observe.call.observation import freeze, grade                   # noqa: E402
from frf.observe.call.runner import Subject                              # noqa: E402

# One subject, nine times. It sums a list of numbers, and REFUSES an empty one -- the refusal is
# what makes this a real test of the wire rather than of arithmetic, because a refusal has to
# arrive as an answer in every one of these languages without killing the process.
SUBJECTS = {
    "python": ("subject.py", '''\
def entry(args):
    values = args[0]
    if not values:
        raise ValueError("empty")
    return sum(values)
'''),
    "javascript": ("subject.js", '''\
module.exports.entry = (args) => {
  const values = args[0];
  if (values.length === 0) { throw new Error('empty'); }
  return values.reduce((a, b) => a + b, 0);
};
'''),
    "ruby": ("subject.rb", '''\
def entry(args)
  values = args[0]
  raise ArgumentError, 'empty' if values.empty?
  values.sum
end
'''),
    "go": ("subject.go", '''\
package main

import "errors"

func Entry(args []interface{}) (interface{}, error) {
	values, ok := args[0].([]interface{})
	if !ok || len(values) == 0 {
		return nil, errors.New("empty")
	}
	total := 0.0
	for _, v := range values {
		total += v.(float64)
	}
	return total, nil
}
'''),
    "rust": ("subject.rs", '''\
pub fn entry(args: &crate::Json) -> Result<crate::Json, String> {
    let items = match args {
        crate::Json::Array(items) => items,
        _ => return Err("empty".to_string()),
    };
    let values = match items.first() {
        Some(crate::Json::Array(values)) if !values.is_empty() => values,
        _ => return Err("empty".to_string()),
    };
    let mut total = 0.0;
    for value in values {
        if let crate::Json::Number(n) = value {
            total += n;
        }
    }
    Ok(crate::Json::Number(total))
}
'''),
    "c": ("subject.c", '''\
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

char *entry_error = NULL;

/* The arguments arrive as JSON text. Summing them by scanning for numbers is enough for a subject
   whose only job is to be somebody else's code in another language. */
const char *entry(const char *args_json)
{
    const char *p = args_json;
    double total = 0.0;
    int seen = 0;
    char *out;

    while (*p) {
        if ((*p >= '0' && *p <= '9') || (*p == '-' && p[1] >= '0' && p[1] <= '9')) {
            char *end;
            total += strtod(p, &end);
            p = end;
            seen = 1;
        } else {
            p++;
        }
    }
    if (!seen) {
        entry_error = "empty";
        return NULL;
    }
    out = malloc(64);
    snprintf(out, 64, "%.17g", total);
    return out;
}
'''),
    "java": ("Subject.java", '''\
import java.util.List;

public class Subject {
    public static Object entry(List<Object> args) {
        List<?> values = (List<?>) args.get(0);
        if (values.isEmpty()) {
            throw new IllegalArgumentException("empty");
        }
        double total = 0.0;
        for (Object value : values) {
            total += ((Number) value).doubleValue();
        }
        return total;
    }
}
'''),
}

# The probes every language answers. The last one is empty, so every subject must refuse it.
PROBES = [[[1, 2, 3]], [[10, 20, 30, 40]], [[7]], [[]]]


def _languages() -> list:
    """Which languages can actually be exercised on this machine."""
    return sorted(name for name in SUBJECTS if shims.usable(name))


def _serve(work: str, language: str) -> list:
    """Write the subject and its shim, build if the language needs it. -> argv that serves it."""
    shim = shims.load(language)
    filename, body = SUBJECTS[language]
    with open(os.path.join(work, filename), "w", encoding="utf-8") as handle:
        handle.write(body)
    with open(os.path.join(work, shim.template), "w", encoding="utf-8") as handle:
        handle.write(shims.source(shim))

    build, argv = shim.commands(work)
    for command in build:
        done = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=600)
        assert done.returncode == 0, "%s did not build: %s" % (language, done.stderr[-1500:])
    return argv


@pytest.mark.parametrize("language", _languages())
def test_a_subject_in_this_language_answers_the_same_wire(language):
    """Every language, one at a time: values come back, and a refusal comes back as an answer.

    The refusal is the half that a careless shim gets wrong, and getting it wrong does not look like
    a bug -- the process simply dies, and the corpus that was being frozen becomes a shorter corpus.
    """
    with tempfile.TemporaryDirectory() as work:
        argv = _serve(work, language)
        with Subject(argv, cwd=work) as subject:
            assert subject.call("entry", [[1, 2, 3]]).value == 6
            assert subject.call("entry", [[10, 20, 30, 40]]).value == 100

            refused = subject.call("entry", [[]])
            assert not refused.ok, "%s answered an empty list instead of refusing" % language
            assert "empty" in refused.error, refused.error
            assert not refused.error.startswith("/") and work not in refused.error, (
                "%s put a host path in its error, which no other machine could reproduce"
                % language)

            # The process survived the refusal, which is the property that makes a refusal an
            # answer rather than the end of the corpus.
            assert subject.call("entry", [[7]]).value == 7


@pytest.mark.parametrize("language", _languages())
def test_a_subject_in_this_language_freezes_and_grades_identically(language):
    """The pipeline's own machinery -- freeze five times, grade -- driven in each language.

    This is the assertion that matters more than the round trip above: it is not enough that each
    language can answer, it has to answer the SAME, or a task's expectations would encode which
    language the reference happened to be written in.
    """
    with tempfile.TemporaryDirectory() as work:
        argv = _serve(work, language)
        observed = []
        for _ in range(3):
            with Subject(argv, cwd=work) as subject:
                observed.append([subject.call("entry", probe) for probe in PROBES])

        for index in range(len(PROBES)):
            expectation = freeze("probe-%d" % index, [run[index] for run in observed])
            assert expectation is not None, (
                "%s did not repeat itself on probe %d, so nothing could be frozen"
                % (language, index))
            passed, total, _ = grade(expectation, observed[0][index])
            assert (passed, total) == (1, 1), (language, index)


@pytest.mark.parametrize("language", _languages())
def test_a_pathological_line_is_skipped_rather_than_fatal(language):
    """A line nested ten thousand deep must not take the process down with it.

    THIS FOUND A REAL ASYMMETRY. The C and Rust shims cap parser depth; the Java one did not, so a
    deeply nested line raised StackOverflowError out of its reader and killed the JVM. That is not
    a refusal -- it is the end of the corpus, and every probe after it is lost. The failure looked
    like a subject that "exited without answering", which sends a repair loop to inspect the wire.

    The rule the contract states is that an unreadable line is SKIPPED and the next is served
    normally, so that is what is asserted: the second probe must still be answered.
    """
    with tempfile.TemporaryDirectory() as work:
        argv = _serve(work, language)
        nested = "[" * 100000 + "]" * 100000
        script = ('{"id":1,"op":"run","call":"entry","args":%s}\n'
                  '{"id":2,"op":"run","call":"entry","args":[[1,2,3]]}\n' % nested)

        done = subprocess.run(argv, cwd=work, input=script, capture_output=True,
                              text=True, timeout=300)

        assert done.returncode == 0, (
            "%s exited %d on a pathological line instead of skipping it: %s"
            % (language, done.returncode, done.stderr[-400:]))
        assert '"id":2' in done.stdout.replace(" ", ""), (
            "%s never answered the probe after the bad line, so the corpus would stop there: %r"
            % (language, done.stdout[-300:]))


def test_the_seam_really_was_exercised_in_more_than_one_language():
    """A suite that skipped its way down to Python would pass while proving nothing.

    This is the guard against the failure mode this file is most likely to develop: toolchains
    disappear from a machine quietly, and every parametrised test above would simply stop existing.
    """
    languages = _languages()
    assert len(languages) >= 2, (
        "only %s could be exercised here, so nothing was demonstrated about language independence. "
        "Install another toolchain, or accept that this run did not check the claim." % languages)


def test_every_language_agrees_on_the_answer():
    """The same probe, in every available language, must produce the same value.

    If two languages disagree, a task's expectations are a record of which language wrote the
    reference -- and a correct reimplementation in the other one would fail a corpus it should pass.
    """
    answers = {}
    for language in _languages():
        with tempfile.TemporaryDirectory() as work:
            argv = _serve(work, language)
            with Subject(argv, cwd=work) as subject:
                answers[language] = [subject.call("entry", probe).value for probe in PROBES[:3]]

    distinct = {tuple(values) for values in answers.values()}
    assert len(distinct) == 1, (
        "the languages do not agree, so an expectation would encode which one wrote it: %s"
        % answers)


def test_the_pipeline_never_learns_which_language_it_served():
    """The mechanical form of the claim: nothing outside the shim table names a language.

    Checked as text because that is what makes it a claim about the CODE rather than about this
    run. A branch on `if language == "go"` anywhere in the seam or the pipeline would be the moment
    "any language" became "the languages somebody remembered".
    """
    import re

    named = re.compile(r"""["'](?:go|rust|ruby|java|javascript|typescript|cpp)["']""")
    offenders = []
    for area in ("core", "observe"):
        base = os.path.join(ROOT, "frf", area)
        for directory, _, files in os.walk(base):
            # The shim table and the coverage table are where languages are ALLOWED to be named:
            # they are the data that makes the rest of the code able to avoid naming any.
            if os.path.basename(directory) in ("shims", "coverage"):
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                for number, line in enumerate(open(path, encoding="utf-8"), 1):
                    code = line.split("#", 1)[0]
                    if named.search(code):
                        offenders.append("%s:%d %s" % (os.path.relpath(path, ROOT), number,
                                                       code.strip()[:70]))
    assert not offenders, offenders
