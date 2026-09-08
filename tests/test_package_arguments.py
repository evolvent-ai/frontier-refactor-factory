"""Path-shaped API data must not be confused with undeclared filesystem dependencies."""
import json

import pytest

from frf.scales.package import _as_argument_lists


def test_absolute_path_strings_remain_valid_probe_data():
    probes = [['normalize_path', '/api/v1/../v2'],
              ['match', '/^items/'],
              ['encode', {'path': '/products/42', 'aliases': ['/a', '/b']}]]
    assert _as_argument_lists(probes) is probes
    assert _as_argument_lists(json.dumps(probes)) == probes


@pytest.mark.parametrize('invalid', [{'args': []}, [None], [['valid'], {'invalid': []}]])
def test_malformed_probe_structure_is_still_rejected(invalid):
    with pytest.raises(ValueError):
        _as_argument_lists(invalid)
