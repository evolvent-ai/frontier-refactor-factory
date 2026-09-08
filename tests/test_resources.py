import pytest

from frf.core import resources


def test_resource_admission_refuses_before_allocating_work(monkeypatch):
    monkeypatch.setattr(resources, 'snapshot', lambda paths: {
        'available_memory': resources.GiB, 'swap_used': 0,
        'disks': {'/': {'free': 50 * resources.GiB}}})
    with pytest.raises(resources.ResourcePressure, match='memory'):
        resources.require_headroom('/tmp')


def test_resource_admission_accounts_for_transfer_buffers(monkeypatch):
    monkeypatch.setattr(resources, 'snapshot', lambda paths: {
        'available_memory': 3 * resources.GiB, 'swap_used': 0,
        'disks': {'/': {'free': 50 * resources.GiB}}})
    resources.require_headroom('/tmp')
    with pytest.raises(resources.ResourcePressure):
        resources.require_headroom('/tmp', transfer_bytes=resources.GiB)


def test_resource_admission_refuses_low_disk(monkeypatch):
    monkeypatch.setattr(resources, 'snapshot', lambda paths: {
        'available_memory': 10 * resources.GiB, 'swap_used': 0,
        'disks': {'/data': {'free': resources.GiB}}})
    with pytest.raises(resources.ResourcePressure, match='disk'):
        resources.require_headroom('/data')
