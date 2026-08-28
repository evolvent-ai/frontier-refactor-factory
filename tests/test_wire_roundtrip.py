"""The wire, exercised end to end in every language the host can actually build.

`test_shims.py` checks that each language's template EXISTS and that the registry is consistent, and
it runs one real round-trip -- in Python, where the factory and the subject share a runtime. That
leaves the interesting half unchecked: the shims exist so that a subject in ANOTHER language can be
served, and "the template is on disk" says nothing about whether the protocol survives a compiler.

So each language here builds a real subject, speaks the real protocol over a real pipe, and has to
agree with every other language on the answers. Divergence between languages is the failure this
catches: a shim that returns 6 for `[[1,2,3]]` and a shim that returns "6" are both plausible on
their own, and only comparing them shows that one of them will grade as wrong against a frozen
expectation.

Languages whose toolchain is absent are SKIPPED rather than assumed, and the skip is reported by
name. An absent compiler is a fact about this host; pretending the language passed would be a claim
about the factory.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frf.observe.call import shims                                          # noqa: E402
from frf.observe.call.runner import Subject                                 # noqa: E402

# One function, eight ways: sum a JSON array of numbers. Deliberately trivial, because what is under
# test is the WIRE and not the arithmetic -- and deliberately not empty, because a subject that
# returns a constant would pass a single-probe check.
SUBJECTS = {
    "python": (
        "def entry(args):\n"
        "    return sum(args)\n"
    ),
    "javascript": (
        "exports.entry = function (args) {\n"
        "  return args.reduce((a, b) => a + b, 0);\n"
        "};\n"
    ),
    # C and C++ share serve.c, so the subject speaks JSON text directly and signals refusal by
    # setting `entry_error` and returning NULL -- the C-shaped version of raising.
    "c": (
        '#include <stdlib.h>\n'
        '#include <stdio.h>\n'
        'char *entry_error = NULL;\n'
        'const char *entry(const char *args_json) {\n'
        '    if (!args_json || args_json[0] != \'[\') {\n'
        '        entry_error = "expected a JSON array";\n'
        '        return NULL;\n'
        '    }\n'
        '    long total = 0; const char *p = args_json;\n'
        '    while (*p) {\n'
        '        if (*p >= \'0\' && *p <= \'9\') {\n'
        '            long v = 0;\n'
        '            while (*p >= \'0\' && *p <= \'9\') { v = v * 10 + (*p - \'0\'); p++; }\n'
        '            total += v; continue;\n'
        '        }\n'
        '        p++;\n'
        '    }\n'
        '    char *out = malloc(64);\n'
        '    snprintf(out, 64, "%ld", total);\n'
        '    return out;\n'
        '}\n'
    ),
    # Go compiles the subject into the shim's own `package main`, so the subject is a single file and
    # the exported name has to be capitalised.
    "go": (
        'package main\n'
        '\n'
        'import "errors"\n'
        '\n'
        'func Entry(args []interface{}) (interface{}, error) {\n'
        '\tif len(args) != 1 {\n'
        '\t\treturn nil, errors.New("expected one argument")\n'
        '\t}\n'
        '\titems, ok := args[0].([]interface{})\n'
        '\tif !ok {\n'
        '\t\treturn nil, errors.New("expected an array")\n'
        '\t}\n'
        '\ttotal := 0.0\n'
        '\tfor _, v := range items {\n'
        '\t\tn, ok := v.(float64)\n'
        '\t\tif !ok {\n'
        '\t\t\treturn nil, errors.New("expected a number")\n'
        '\t\t}\n'
        '\t\ttotal = total + n\n'
        '\t}\n'
        '\treturn total, nil\n'
        '}\n'
    ),
    # Java's subject is a class with a static method, compiled alongside the shim. Sums into a long
    # rather than a double on purpose: the probes are integers, and a subject that answered 6.0 where
    # every other language answers 6 would be graded against a frozen integer expectation.
    # TypeScript compiles to JS, then serves through the same shim; only the build step differs.
    "typescript": (
        'export function entry(args: number[]): number {\n'
        '  return args.reduce((a: number, b: number) => a + b, 0);\n'
        '}\n'
    ),
    "java": (
        'import java.util.List;\n'
        '\n'
        'public class Subject {\n'
        '    public static Object entry(List<Object> args) {\n'
        '        if (args.size() != 1 || !(args.get(0) instanceof List)) {\n'
        '            throw new IllegalArgumentException("expected one array argument");\n'
        '        }\n'
        '        long total = 0;\n'
        '        for (Object v : (List<?>) args.get(0)) {\n'
        '            total += ((Number) v).longValue();\n'
        '        }\n'
        '        return total;\n'
        '    }\n'
        '}\n'
    ),
    # Rust's subject is compiled in as `mod subject`, so it speaks the shim's own `Json` type and
    # signals refusal with `Err`. The `?` operator makes each shape check its own refusal.
    # Ruby's subject is required alongside the shim and its parameters are splatted from the JSON
    # list, so `entry(args)` receives the whole array as one argument.
    "ruby": (
        'def entry(args)\n'
        '  args.sum\n'
        'end\n'
    ),
    "rust": (
        'pub fn entry(args: &crate::Json) -> Result<crate::Json, String> {\n'
        '    let items = args.as_array().ok_or("expected an array")?;\n'
        '    if items.len() != 1 {\n'
        '        return Err("expected one argument".to_string());\n'
        '    }\n'
        '    let inner = items[0].as_array().ok_or("expected an array")?;\n'
        '    let mut total = 0.0;\n'
        '    for v in inner {\n'
        '        total += v.as_f64().ok_or("expected a number")?;\n'
        '    }\n'
        '    Ok(crate::Json::Number(total))\n'
        '}\n'
    ),
    "cpp": (
        '#include <cstdlib>\n'
        '#include <cstdio>\n'
        'extern "C" { char *entry_error = nullptr; }\n'
        'extern "C" const char *entry(const char *args_json) {\n'
        '    if (!args_json || args_json[0] != \'[\') {\n'
        '        entry_error = (char *)"expected a JSON array";\n'
        '        return nullptr;\n'
        '    }\n'
        '    long total = 0; const char *p = args_json;\n'
        '    while (*p) {\n'
        '        if (*p >= \'0\' && *p <= \'9\') {\n'
        '            long v = 0;\n'
        '            while (*p >= \'0\' && *p <= \'9\') { v = v * 10 + (*p - \'0\'); p++; }\n'
        '            total += v; continue;\n'
        '        }\n'
        '        p++;\n'
        '    }\n'
        '    char *out = (char *)malloc(64);\n'
        '    snprintf(out, 64, "%ld", total);\n'
        '    return out;\n'
        '}\n'
    ),
}

# Argument lists and the answer every language must give. The empty array is here because zero is the
# value a broken accumulator is most likely to return by accident for the others too.
CASES = (
    ([[1, 2, 3]], 6),
    ([[10, 20]], 30),
    ([[]], 0),
    ([[7]], 7),
)

BUILD_TIMEOUT = 120.0

# Go only exports capitalised identifiers, and its subject is compiled into the shim's own package
# rather than loaded, so the seam's name is part of the language rather than a convention we pick.
SYMBOLS = {"go": "Entry"}


def _symbol(language: str) -> str:
    return SYMBOLS.get(language, "entry")


def _serve(language: str, source: str, directory: str) -> Subject:
    """Materialise the shim, build if the language needs building, return an unstarted Subject."""
    shim = shims.TEMPLATES[language]
    path = os.path.join(directory, shim.subject)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    builds, argv = shims.materialise(directory, language, path, _symbol(language))
    for command in builds:
        done = subprocess.run(list(command), cwd=directory, capture_output=True,
                              text=True, timeout=BUILD_TIMEOUT)
        assert done.returncode == 0, (
            "%s subject did not build: %s" % (language, done.stderr.strip()[:400]))
    return Subject(argv, cwd=directory, timeout=60.0)


@pytest.mark.parametrize("language", sorted(SUBJECTS))
def test_a_subject_answers_over_the_wire(language):
    """The protocol survives the trip out of the factory's runtime and back."""
    if not shims.usable(language):
        pytest.skip("%s toolchain (%s) is not installed on this host"
                    % (language, shims.TEMPLATES[language].tool))
    with tempfile.TemporaryDirectory() as directory:
        subject = _serve(language, SUBJECTS[language], directory)
        with subject as served:
            for arguments, expected in CASES:
                observed = served.call(_symbol(language), arguments)
                assert observed.ok, (
                    "%s refused %r: %s" % (language, arguments, observed.error))
                assert observed.value == expected, (
                    "%s answered %r for %r, expected %r"
                    % (language, observed.value, arguments, expected))


def test_the_languages_agree_with_each_other():
    """Two shims that disagree cannot both grade against one frozen expectation.

    This is the check that a per-language test cannot make: a shim returning the string "6" passes
    its own round-trip and fails every comparison against a frozen integer.
    """
    answers: dict = {}
    for language, source in sorted(SUBJECTS.items()):
        if not shims.usable(language):
            continue
        with tempfile.TemporaryDirectory() as directory:
            with _serve(language, source, directory) as served:
                answers[language] = [served.call(_symbol(language), arguments).value
                                     for arguments, _ in CASES]

    if len(answers) < 2:
        pytest.skip("fewer than two toolchains available; nothing to compare")

    reference_language, reference = sorted(answers.items())[0]
    for language, got in sorted(answers.items()):
        assert got == reference, (
            "%s answered %r but %s answered %r for the same probes"
            % (language, got, reference_language, reference))
    # And the answers must be the right TYPE, not merely consistent: every shim that agreed on the
    # string "6" would still fail against a frozen integer expectation.
    assert all(isinstance(value, int) for value in reference), (
        "answers are not integers: %r" % (reference,))


@pytest.mark.parametrize("language", sorted(SUBJECTS))
def test_args_of_the_wrong_shape_are_refused_not_silently_reshaped(language):
    """Every shim had its own way of mishandling this, and each way cost something different.

    Sent `{"args": "not-a-list"}`, the eight shims used to do five different things:

      * C, C++, Ruby, Rust, Java rewrote it into a call with NO arguments, so the subject answered
        whatever a no-argument call returns and the reply said ok:true. A false SUCCESS is the
        expensive one -- it enters grading as though it were a real answer.
      * Go wrote nothing at all and kept reading, so the factory blocked on an id that never came
        back until PROBE_TIMEOUT, which defaults to two minutes per occurrence.
      * Python spread the string into its characters, and the refusal it produced said "entry()
        takes 1 positional argument but 10 were given" -- a sentence about the SUBJECT'S signature
        describing a defect in the request, which would then be frozen into an expectation.

    All three are the same bug: a malformed request is the factory's fault, and only an explicit
    refusal says so. This test is parametrised over every language whose toolchain is present, which
    is what turned the bug up -- each shim looked defensible until they were compared.
    """
    if not shims.usable(language):
        pytest.skip("%s toolchain (%s) is not installed on this host"
                    % (language, shims.TEMPLATES[language].tool))
    with tempfile.TemporaryDirectory() as directory:
        with _serve(language, SUBJECTS[language], directory) as served:
            # Sanity first: the subject answers a well-formed call, so a refusal below is about the
            # malformed one rather than about a broken build.
            assert served.call(_symbol(language), [[1, 2, 3]]).value == 6

            refused = served.call(_symbol(language), "not-a-list")
            assert not refused.ok, (
                "%s accepted a non-array `args` and answered %r -- a false success"
                % (language, refused.value))
            assert refused.error, "%s refused without saying why" % language
            # The reason must not blame the subject's signature for a malformed request.
            assert "positional argument" not in (refused.error or ""), (
                "%s blamed the subject's signature for a malformed request: %s"
                % (language, refused.error))


@pytest.mark.parametrize("language", sorted(SUBJECTS))
def test_an_unreadable_line_is_skipped_rather_than_answered(language):
    """The other half of the same rule: silence is correct for a line that is not a call.

    Refusing everything unparseable would be just as wrong in the other direction -- a blank line or
    a stray log line has no id, so there is nothing to answer and no one waiting. What must survive
    is the call that FOLLOWS it.
    """
    if not shims.usable(language):
        pytest.skip("%s toolchain (%s) is not installed on this host"
                    % (language, shims.TEMPLATES[language].tool))
    # Driven with a raw pipe rather than through `Subject`, which only speaks well-formed calls.
    # Adding a "send arbitrary bytes" method to the production runner just to reach this case would
    # put a test's needs into the seam every real probe goes through.
    with tempfile.TemporaryDirectory() as directory:
        subject = _serve(language, SUBJECTS[language], directory)
        request = '{"id": 7, "call": "%s", "args": [[4, 5]]}' % _symbol(language)
        done = subprocess.run(subject.argv, cwd=directory, text=True,
                              input="this line is not json\n" + request + "\n",
                              capture_output=True, timeout=60)
        replies = [line for line in done.stdout.splitlines() if line.strip()]
        assert len(replies) == 1, (
            "%s wrote %d replies to one unreadable line plus one call: %r"
            % (language, len(replies), replies))
        assert '"ok": true' in replies[0].replace('"ok":true', '"ok": true'), (
            "%s lost the call that followed an unreadable line: %r" % (language, replies[0]))
        assert "9" in replies[0], (
            "%s answered %r, expected the sum 9" % (language, replies[0]))


def test_a_refusal_crosses_the_wire_as_a_refusal():
    """Error paths are part of a contract, so the wire has to carry "no" as well as a value.

    A shim that turned a refusal into a null VALUE would make every candidate that correctly rejects
    bad input look like a candidate that returned nothing.
    """
    language = "python"
    if not shims.usable(language):
        pytest.skip("python toolchain is not available")
    source = ("def entry(args):\n"
              "    if not isinstance(args, list):\n"
              "        raise TypeError('expected a list')\n"
              "    return sum(args)\n")
    with tempfile.TemporaryDirectory() as directory:
        with _serve(language, source, directory) as served:
            refused = served.call("entry", "not-a-list")
            assert not refused.ok, "a raising subject was reported as a success"
            assert refused.error, "a refusal crossed the wire with no reason attached"
