FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Docker CE (daemon + CLI) on Ubuntu — avoids Alpine package conflicts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*

# Python for the validation script
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Start dockerd on container start (e2b will exec commands into this container)
RUN echo '#!/bin/bash\ndockerd &>/var/log/dockerd.log &\nsleep 2\nexec "$@"' \
    > /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

WORKDIR /workspace
