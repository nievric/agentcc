# Pinned index digest for codercom/code-server:4.121.0, resolved for the 0.1.0
# release. Refresh the tag and digest together for a later release.
ARG CODE_SERVER_VERSION=4.121.0
ARG VERSION=dev
ARG REVISION=unknown
FROM codercom/code-server:${CODE_SERVER_VERSION}@sha256:ea5f42d99998e2ba44297eca9b8a38a101e718ef54caaa41d649f6c6f1c8c71e

ARG VERSION
ARG REVISION

LABEL org.opencontainers.image.title="AgentCC session base" \
      org.opencontainers.image.description="AgentCC common isolated session image with code-server" \
      org.opencontainers.image.source="https://github.com/nievric/agentcc" \
      org.opencontainers.image.url="https://github.com/nievric/agentcc" \
      org.opencontainers.image.documentation="https://github.com/nievric/agentcc" \
      org.opencontainers.image.vendor="Nievric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

USER root

# Common tooling only. Agent CLIs and language SDKs are installed by derived
# harness/template images so unused runtimes never inflate every session.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        openssh-client \
        ripgrep \
        tini \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# The upstream code-server image includes a passwordless sudo rule for `coder`.
# Session users must stay unprivileged: an agent with root inside a live
# container could inspect the harness process and its scoped provider secret.
RUN rm -f /etc/sudoers.d/nopasswd

# `coder` owns code-server. Each harness runs under its own unprivileged user
# and shares only the explicitly prepared workspace group.
RUN groupadd --gid 10001 workspace \
    && usermod --append --groups workspace coder \
    && useradd --uid 10001 --gid workspace --create-home --shell /bin/bash agent \
    && install --directory --owner coder --group workspace --mode 2775 /workspaces/session \
    && install --directory --owner coder --group coder --mode 0755 /opt/code-server/extensions

COPY --chmod=755 entrypoint.sh /usr/local/bin/agentcc-session

ENV HOME=/home/coder \
    CODE_SERVER_PORT=8080 \
    CODE_SERVER_DATA_DIR=/tmp/code-server \
    CODE_SERVER_EXTENSIONS_DIR=/opt/code-server/extensions \
    WORKSPACE_ROOT=/workspaces/session

WORKDIR /workspaces/session

# The runtime must not publish this port. The authenticated workspace gateway
# reaches it across the private container network.
EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agentcc-session"]
