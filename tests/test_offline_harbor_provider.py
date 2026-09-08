import pytest

from harbor.models.task.config import NetworkMode, NetworkPolicy
from runtime.benchmark_docker import OfflineDockerEnvironment


@pytest.mark.parametrize('mode', [NetworkMode.PUBLIC, NetworkMode.ALLOWLIST])
def test_fixed_network_provider_refuses_other_policies(mode):
    with pytest.raises(ValueError, match='only supports'):
        OfflineDockerEnvironment._check_policy(NetworkPolicy(network_mode=mode))


def test_fixed_provider_only_claims_static_isolation():
    provider = object.__new__(OfflineDockerEnvironment)
    provider._enable_egress_control = False
    capabilities = provider.capabilities
    assert capabilities.disable_internet
    assert not capabilities.dynamic_network_policy
    assert not capabilities.network_allowlist
    assert not capabilities.docker_compose
    OfflineDockerEnvironment._check_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
