#!/usr/bin/env python3
"""Is this E2B key usable? -> a pass/fail line per capability, and exit 0 only if all passed.

Standalone on purpose: it imports nothing from frf or swe_evolvent, so a failure here is the key
or the network, never our wiring. It does mirror the two habits both projects learned the hard way:

  * EVERY SDK CALL GETS A `request_timeout`. The SDK takes two independent deadlines -- `timeout`
    bounds the process inside the sandbox, `request_timeout` bounds the HTTP call carrying it.
    Passing only the first leaves the wire unbounded, and a dead connection then hangs forever
    instead of failing.
  * `Sandbox.create(...)`, not `Sandbox(...)`. In the 2.x SDK the constructor takes an internal
    options object; calling it with keywords fails at first use.

A GATEWAY IS TWO URLS, NOT ONE, and that is the thing worth knowing before reading a failure here.
`api_url` reaches the control plane (create/list/kill); the command and file channels talk to the
sandbox itself at a host the SDK derives from `domain`, which defaults to `e2b.app` no matter what
`api_url` says. So a deployment behind a gateway can pass every control-plane check and still fail
the moment it runs a command -- and the fix is `E2B_DOMAIN`/`E2B_SANDBOX_URL`, not the key. The
checks below are ordered to make that split visible instead of reporting one undifferentiated fail.

Usage:  python check_e2b_key.py <e2b_key> [--api-url <url>] [--template <id>]
        E2B_API_URL=... E2B_API_KEY=... python check_e2b_key.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

OPEN_TIMEOUT = 120.0      # creating + connecting a sandbox
CALL_TIMEOUT = 60.0       # one ordinary command or file transfer
TEARDOWN_TIMEOUT = 20.0   # killing is bounded short: failing it costs nothing, it expires anyway
LIFETIME = 120            # seconds the sandbox stays alive; the API rejects anything over 1 hour

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print("  %s  %-22s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("key", nargs="?", default=os.environ.get("E2B_API_KEY", ""))
    parser.add_argument("--api-url", default=os.environ.get("E2B_API_URL", ""),
                        help="control-plane base URL; omit for E2B's own cloud")
    parser.add_argument("--domain", default=os.environ.get("E2B_DOMAIN", ""),
                        help="sandbox host domain; the SDK defaults to e2b.app regardless of --api-url")
    parser.add_argument("--template", default=os.environ.get("E2B_TEMPLATE", ""),
                        help="template id to open instead of the default base image")
    args = parser.parse_args()

    # Set as environment too, not only passed as keywords: the SDK rebuilds a throwaway config for
    # some instance methods, and one that reads a different URL than `create` did would talk to the
    # wrong control plane halfway through the run.
    if args.api_url:
        os.environ["E2B_API_URL"] = args.api_url
    if args.domain:
        os.environ["E2B_DOMAIN"] = args.domain
    # Options every SDK call takes, so the gateway is not re-specified (or forgotten) per call.
    conn = {"api_key": args.key}
    if args.api_url:
        conn["api_url"] = args.api_url
    if args.domain:
        conn["domain"] = args.domain

    if not args.key:
        print("no key: pass it as argv[1] or set E2B_API_KEY", file=sys.stderr)
        return 2

    try:
        from e2b import Sandbox
    except ImportError:
        print("the e2b package is not installed for %s" % sys.executable, file=sys.stderr)
        return 2

    import importlib.metadata as metadata
    print("e2b SDK %s | python %s" % (metadata.version("e2b"), sys.version.split()[0]))
    print("key      %s...%s" % (args.key[:12], args.key[-4:]))
    print("api url  %s" % (args.api_url or "https://api.e2b.app (SDK default)"))
    print("domain   %s%s" % (args.domain or "e2b.app (SDK default)",
                             "" if args.domain else "   <- sandbox hosts come from here, not api url"))
    if args.template:
        print("template %s" % args.template)
    print()

    # 1. AUTH, and nothing else. The cheapest call that proves the key is accepted: a rejected key
    #    fails here in a second, rather than after paying for a sandbox that was never going to open.
    started = time.perf_counter()
    try:
        page = Sandbox.list(request_timeout=CALL_TIMEOUT, **conn)
        live = 0
        while True:
            live += len(page.next_items())
            if not page.has_next:
                break
        record("auth/list", True, "%d live sandbox(es), %.1fs" % (live, time.perf_counter() - started))
    except Exception as exc:                              # noqa: BLE001 -- the SDK's own errors
        record("auth/list", False, str(exc)[:400])
        print("\nthe key was not accepted; nothing below would tell you more.")
        return 1

    # 2. OPEN ONE. Separate from auth because a valid key can still fail here -- an exhausted quota,
    #    a template this key cannot see, a regional outage -- and those are different problems.
    sandbox = None
    started = time.perf_counter()
    try:
        if args.template:
            sandbox = Sandbox.create(template=args.template, timeout=LIFETIME,
                                     request_timeout=OPEN_TIMEOUT, **conn)
        else:
            sandbox = Sandbox.create(timeout=LIFETIME, request_timeout=OPEN_TIMEOUT, **conn)
        record("create", True, "id=%s, %.1fs" % (getattr(sandbox, "sandbox_id", "?"),
                                                 time.perf_counter() - started))
        # WHERE THE COMMAND CHANNEL WILL ACTUALLY GO. Printed before the first command runs, so a
        # connection failure below can be read against the host it was aimed at rather than guessed.
        print("       sandbox host: %s" % getattr(sandbox, "envd_api_url", "?"))
    except Exception as exc:                              # noqa: BLE001
        record("create", False, str(exc)[:400])
        return 1

    try:
        # 3. RUN SOMETHING and read it back. Proves the command channel, not just the control plane.
        started = time.perf_counter()
        try:
            done = sandbox.commands.run("echo e2b-ok && uname -sm && python3 -V 2>&1 | head -1",
                                        timeout=30, request_timeout=CALL_TIMEOUT)
            out = " / ".join(line for line in (done.stdout or "").split("\n") if line.strip())
            record("run", done.exit_code == 0 and "e2b-ok" in (done.stdout or ""),
                   "%s (%.1fs)" % (out[:120], time.perf_counter() - started))
        except Exception as exc:                          # noqa: BLE001
            record("run", False, str(exc)[:300])

        # 4. A NON-ZERO EXIT ARRIVES AS AN EXCEPTION in this SDK, which is the single behaviour most
        #    likely to be mis-wired by a caller: an ordinary failed build looks like a transport
        #    fault. Checked here so the answer is on record rather than rediscovered in a batch.
        try:
            sandbox.commands.run("exit 3", timeout=30, request_timeout=CALL_TIMEOUT)
            record("nonzero-exit", False, "returned instead of raising -- SDK behaviour changed")
        except Exception as exc:                          # noqa: BLE001
            code = getattr(exc, "exit_code", None)
            record("nonzero-exit", code == 3,
                   "raises %s carrying exit_code=%s" % (type(exc).__name__, code))

        # 5. FILES BOTH WAYS. push/pull in both projects is a tar over this API, so a key that can
        #    run but not transfer would fail at the first real stage instead of here.
        try:
            payload = b"round-trip-" + os.urandom(4).hex().encode()
            sandbox.files.write("/tmp/e2b_check.bin", payload, request_timeout=CALL_TIMEOUT)
            back = bytes(sandbox.files.read("/tmp/e2b_check.bin", format="bytes",
                                            request_timeout=CALL_TIMEOUT))
            record("files", back == payload, "wrote and read %d bytes" % len(payload))
        except Exception as exc:                          # noqa: BLE001
            record("files", False, str(exc)[:300])

        # 6. OUTBOUND NETWORK from inside. Every real stage installs something; a sandbox that opens
        #    but cannot reach a registry fails much later and much more expensively.
        try:
            done = sandbox.commands.run(
                "curl -sS -o /dev/null -w '%{http_code}' --max-time 20 https://pypi.org/simple/",
                timeout=40, request_timeout=CALL_TIMEOUT)
            record("egress", (done.stdout or "").strip().startswith("2"),
                   "pypi.org -> HTTP %s" % (done.stdout or "").strip())
        except Exception as exc:                          # noqa: BLE001
            record("egress", False, str(exc)[:200])
    finally:
        # Bounded short and never fatal: an unkilled sandbox expires on its own, and raising here
        # would turn a passing check into a failure during cleanup.
        try:
            sandbox.kill(request_timeout=TEARDOWN_TIMEOUT)
            record("kill", True, "")
        except Exception as exc:                          # noqa: BLE001
            record("kill", False, "%s (it will expire in %ds anyway)" % (str(exc)[:150], LIFETIME))

    failed = [name for name, ok, _ in results if not ok]
    print("\n%s -- %d/%d checks passed%s"
          % ("KEY IS USABLE" if not failed else "KEY HAS PROBLEMS",
             len(results) - len(failed), len(results),
             "" if not failed else "; failed: " + ", ".join(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
