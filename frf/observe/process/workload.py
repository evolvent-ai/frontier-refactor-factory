"""Minimum evidence required for a process scenario to measure useful execution.

This excludes control queries and proven failed execution. It is a necessary check, not a
proof of repository-wide coverage or performance significance.
"""
import hashlib


_CONTROL = {"--help", "-h", "--version", "-V", "help", "version"}


def command_family(scenario, input_path=None):
    words = []
    for arg in scenario['steps'][0].get('argv', [])[1:3]:
        if (arg == input_path or arg.startswith('-') or arg.isdigit()
                or any(mark in arg for mark in ('/', '.', ' '))):
            break
        words.append(arg)
    return ' '.join(words) or 'direct-input'


def control_step(step):
    args = step.get("argv") or []
    if not args or args[0] != "{PROGRAM}":
        return False
    options = args[1:args.index("--")] if "--" in args else args[1:]
    return options == ["-v"] or any(arg in _CONTROL for arg in options)


def output_signature(expectation):
    channels = [expectation.get(name, {}) for name in ("exit_code", "stdout", "stderr")]
    if not all(rule.get("graded") and not rule.get("masked") for rule in channels):
        return None
    if not any(rule.get("line_count", 0) > 0 for rule in channels[1:]):
        return None
    return tuple((rule.get("digest"), rule.get("line_count")) for rule in channels)


def control_output_signatures(scenarios, expectations):
    """Stable explicit help/version output identifies disguised control queries too."""
    signatures = set()
    for scenario in scenarios:
        rules = expectations.get(scenario["probe_id"], [])
        for index, step in enumerate(scenario.get("steps", [])):
            if index < len(rules) and control_step(step):
                signature = output_signature(rules[index])
                if signature is not None:
                    signatures.add(signature)
    return signatures


def timing_steps(scenario):
    """Direct subject steps with workload input; shell wrappers require separate evidence."""
    selected = []
    for index, step in enumerate(scenario.get("steps", ())):
        args = step.get("argv") or []
        if not args or args[0] != "{PROGRAM}":
            continue
        if control_step(step):
            continue
        if len(args) > 1 or step.get("stdin") or scenario.get("fixture"):
            selected.append(index)
    return selected


def eligible_timing_steps(scenario, expectations, controls=()):
    """Require stable success on a workload step, not merely printed error text."""
    zero = "sha256:" + hashlib.sha256(b"0").hexdigest()
    selected = []
    for index in timing_steps(scenario):
        if index >= len(expectations):
            continue
        if output_signature(expectations[index]) in controls:
            continue
        rule = expectations[index].get("exit_code", {})
        if (rule.get("graded") and rule.get("digest") == zero
                and rule.get("line_count") == 1 and not rule.get("masked")):
            selected.append(index)
    return selected


def standalone_source():
    from pathlib import Path
    return Path(__file__).read_text(encoding="utf-8")
