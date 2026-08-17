"""Serve a Python subject over the wire. Written into the task; not imported by the factory.

The subject supplies `entry(args) -> value`, raising to refuse. Everything else is this file.
"""
import json
import sys
import time


def serve(entry, stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except (ValueError, RecursionError):
            # RecursionError as well as ValueError, because a line nested deeply enough exhausts
            # the decoder's stack rather than being rejected as malformed -- and RecursionError is
            # not a ValueError, so it escapes and kills the process. That is not a skipped line: it
            # is the end of the corpus, and every probe behind it is lost.
            continue                                  # an unreadable line is not a call
        if not isinstance(request, dict):
            continue                                  # a bare value is not a request either
        rid, op, args = request.get("id"), request.get("op", "run"), request.get("args", [])

        if op == "time":
            # TIMED HERE, on this side of the pipe. Measuring from the factory would charge the
            # subject for process startup and JSON transport, which for a quick subject is most of
            # what the clock would see.
            repeats = int(request.get("repeats", 1))
            failed = False
            started = time.perf_counter()
            for _ in range(repeats):
                try:
                    entry(args)
                except Exception:
                    failed = True
                    break
            elapsed = time.perf_counter() - started
            reply = {"id": rid, "ok": not failed, "seconds": elapsed}
        else:
            try:
                reply = {"id": rid, "ok": True, "value": entry(args)}
            except Exception as exc:
                # A REFUSAL IS AN ANSWER. How the subject rejects bad input is behaviour a
                # reimplementation must reproduce, so this is reported rather than raised.
                # The TYPE and MESSAGE only -- never a traceback. A traceback carries absolute
                # paths from the machine that ran it, which would be frozen into the expectation and
                # then be impossible for any other machine to reproduce.
                reply = {"id": rid, "ok": False,
                         "error": "%s: %s" % (type(exc).__name__, exc)}

        stdout.write(json.dumps(reply, separators=(",", ":"), sort_keys=True) + "\n")
        stdout.flush()


if __name__ == "__main__":
    sys.path.insert(0, ".")
    import importlib

    # WHICH SYMBOL, from the command line. A shim that could only serve a function literally called
    # `entry` could only serve a subject somebody had written for it -- and the whole point is to
    # serve real code, where the function is called `camel_to_snake` and lives beside its own
    # imports. The name travels as an argument rather than being edited into this file, so the
    # template stays data and one copy serves every subject.
    _module = sys.argv[1] if len(sys.argv) > 1 else "subject"
    _symbol = sys.argv[2] if len(sys.argv) > 2 else "entry"
    _entry = getattr(importlib.import_module(_module.removesuffix(".py")), _symbol)

    # The wire always passes ONE argument: the list of arguments. A real function takes them
    # spread out, so the adaptation happens here rather than in every subject -- which would mean
    # editing somebody else's code to serve it, and then grading the edit.
    serve(lambda args: _entry(*args))
