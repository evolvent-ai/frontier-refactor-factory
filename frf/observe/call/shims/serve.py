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
        except ValueError:
            continue                                  # an unreadable line is not a call
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
    from subject import entry                          # noqa: E402  -- written beside this file

    serve(entry)
