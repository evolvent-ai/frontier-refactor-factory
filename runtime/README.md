# Fixed Offline Docker Environment

`benchmark_docker.py` is a Harbor 0.22.0 environment extension for Linux tasks
that require `network_mode = "no-network"`. It uses Docker's fixed `none`
network namespace and verifies isolation before each container is used.
It does not require E2B credentials or a private service.

The reference setup uses Python 3.12, Docker Engine 29.7.2 and Docker Compose
2.39.4 on Linux/amd64. Use the architecture declared by the image receipt.
Docker Compose must be installed as a Docker CLI plugin.

After verifying the release checksums and importing its image archive:

```sh
python3.12 -m venv .runner-venv
.runner-venv/bin/pip install harbor==0.22.0
sha256sum -c checksums.sha256
docker load -i images/task-runtime.tar
PYTHONPATH="$PWD/runtime" .runner-venv/bin/harbor run \
  --path tasks/task-name --agent nop \
  --env benchmark_docker:OfflineDockerEnvironment
```

Use the archive and task paths listed in the release manifest. `nop` leaves
the initial workspace unchanged; its outcome is a runtime diagnostic, not
evidence that an agent solved the task.

The task's native `[environment].docker_image` selects the imported solver
image. Its separate verifier environment builds from the validated local
content tag and copies `tests/` into `/tests`. The solver image must never
contain those evaluator files. A local content tag is intentional: BuildKit
can attempt a registry lookup for `FROM repository@sha256:...` even after
the corresponding image has been loaded locally.

This extension supports one task container with fixed offline execution.
It rejects allowlists, dynamic network policies, Windows containers and
task-authored Compose services. Model communication must occur outside the
offline task container. Agent-specific installation and tool prerequisites
must be prepared before offline execution; the extension does not install
them or open network access for them.

Native baseline and incorrect-submission trials have been exercised. This
does not certify every agent integration, task, numerical contract or release
artifact. Task acceptance and release checks remain separate requirements.
