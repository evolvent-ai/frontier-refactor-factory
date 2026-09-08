"""Harbor 0.22 Docker provider for single-container tasks with fixed network isolation.

Uses Docker's network namespace isolation on kernels without NFT_FIB_INET. Task files remain
native Harbor files. Dynamic policies and task-authored Compose services are deliberately refused.
"""
import json
import shlex
import tempfile
from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import NetworkMode, NetworkPolicy


class OfflineDockerEnvironment(DockerEnvironment):
    def __init__(self, *, task_env_config, network_policy=None, phase_network_policies=(), **kwargs):
        if task_env_config.os != 'linux':
            raise ValueError('fixed-network Docker provider requires Linux')
        policy = network_policy or NetworkPolicy(network_mode=task_env_config.network_mode)
        for item in (policy, *phase_network_policies):
            self._check_policy(item)
        environment_dir = Path(kwargs['environment_dir'])
        if (environment_dir / 'docker-compose.yaml').exists() or kwargs.get('extra_docker_compose'):
            raise ValueError('fixed-network Docker provider does not accept extra Compose services')
        super().__init__(task_env_config=task_env_config, network_policy=policy,
                         phase_network_policies=phase_network_policies, **kwargs)
        if self.extra_docker_compose_paths:
            raise ValueError('fixed-network Docker provider does not accept Compose overrides')
        self._fixed_network_dir = tempfile.TemporaryDirectory(prefix='harbor-fixed-network-')
        self._fixed_network_path = Path(self._fixed_network_dir.name) / 'compose.yaml'
        self._fixed_network_path.write_text('services:\n  main:\n    network_mode: none\n')

    @staticmethod
    def _check_policy(policy):
        if policy.network_mode != NetworkMode.NO_NETWORK or policy.allowed_hosts:
            raise ValueError('fixed-network Docker provider only supports no-network policies')

    @staticmethod
    def _requires_egress_control(*, startup_network_policy, phase_network_policies):
        # This provider enforces fixed network namespaces, not Harbor's nftables sidecar.
        return False

    @property
    def capabilities(self):
        return super().capabilities.model_copy(update={
            'disable_internet': True, 'dynamic_network_policy': False,
            'docker_compose': False, 'windows': False})

    def validate_network_policy_support(self, network_policy=None):
        self._check_policy(network_policy or self.network_policy)
        super().validate_network_policy_support(network_policy)

    @property
    def _docker_compose_paths(self):
        paths = super()._docker_compose_paths
        if hasattr(self, '_fixed_network_path'):
            paths.append(self._fixed_network_path)
        return paths

    async def start(self, force_build):
        await super().start(force_build)
        probe = (
            'import json,socket; from pathlib import Path\n'
            'interfaces=sorted(p.name for p in Path("/sys/class/net").iterdir())\n'
            'try: socket.create_connection(("1.1.1.1",443),timeout=2)\n'
            'except OSError: external=False\n'
            'else: external=True\n'
            'print(json.dumps({"interfaces":interfaces,"external":external}))\n')
        try:
            result = await self.exec('python3 -I -c ' + shlex.quote(probe), timeout_sec=10)
            report = json.loads(result.stdout or '')
            if result.return_code or report != {'interfaces': ['lo'], 'external': False}:
                raise RuntimeError('container did not demonstrate fixed network isolation')
            self.network_isolation_evidence = report
        except BaseException:
            await super().stop(delete=True)
            raise
