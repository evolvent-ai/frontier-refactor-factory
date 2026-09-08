"""Numerical comparison for JSON results, shared with standalone task evaluators."""
import math


def validate_numeric_policy(policy):
    if not isinstance(policy, dict) or policy.get('kind') != 'float-tolerance':
        raise ValueError('invalid numerical comparison policy')
    for key in ('rtol', 'atol'):
        value = policy.get(key)
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('numerical tolerances must be finite nonnegative numbers')
    if not isinstance(policy.get('equal_nan', True), bool):
        raise ValueError('equal_nan must be boolean')
    return policy


def compare_numeric(expected, actual, policy):
    """Floats use atol + rtol * abs(reference); structure and integer values stay exact."""
    validate_numeric_policy(policy)

    def compare(left, right, path):
        if type(left) is float:
            if type(right) not in (int, float):
                return False, path + ': floating-point value required'
            try:
                if math.isnan(left):
                    same = policy.get('equal_nan', True) and math.isnan(right)
                elif math.isinf(left):
                    same = left == right
                elif not math.isfinite(right):
                    same = False
                else:
                    allowed = policy['atol'] + policy['rtol'] * abs(left)
                    same = math.isfinite(allowed) and abs(left - right) <= allowed
            except (OverflowError, ValueError):
                same = False
            return (True, '') if same else (False, path + ': outside numerical contract')
        if type(left) is not type(right):
            return False, path + ': result type differs'
        if isinstance(left, dict):
            if left.keys() != right.keys():
                return False, path + ': result fields differ'
            pairs = ((left[key], right[key], path + '.' + str(key)) for key in left)
        elif isinstance(left, list):
            if len(left) != len(right):
                return False, path + ': result shape differs'
            pairs = ((a, b, path + '[' + str(i) + ']') for i, (a, b) in enumerate(zip(left, right)))
        else:
            return (True, '') if left == right else (False, path + ': exact result differs')
        for a, b, child in pairs:
            same, reason = compare(a, b, child)
            if not same:
                return same, reason
        return True, ''

    return compare(expected, actual, '$')


def numeric_policy_text(policy):
    validate_numeric_policy(policy)
    nan = 'NaN is accepted only at matching NaN positions.' if policy.get('equal_nan', True) else 'NaN results are not accepted.'
    return ('For each floating-point result, abs(candidate - reference) <= %.12g + %.12g * abs(reference). '
            'Integer values, booleans, strings, field names, and array shapes must match exactly. '
            'Infinities must match in sign and position. %s' % (policy['atol'], policy['rtol'], nan))


def source():
    from pathlib import Path
    return Path(__file__).read_text()
