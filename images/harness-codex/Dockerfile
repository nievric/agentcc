ARG BASE_IMAGE=agentcc-session-base:dev
ARG CODEX_VERSION=0.153.4
ARG VERSION=dev
ARG REVISION=unknown
# Pinned index digest for node:22-bookworm-slim, resolved for the 0.1.0 release.
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS codex-installer
ARG CODEX_VERSION

# This stage is discarded. It supplies npm only to install the pinned package;
# the final image reuses code-server's bundled Node runtime.
RUN npm install --global --omit=dev --no-audit --no-fund "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && rm -rf /root/.npm

FROM ${BASE_IMAGE}

ARG VERSION
ARG REVISION

USER root

# Codex uses bubblewrap for its Linux sandbox. Tmux owns the one persistent
# interactive Codex process while browser clients attach and detach. Python is
# included here (rather than in the code-server base) because it is a developer
# runtime needed by this harness profile, not by every possible session image.
# Users install libraries in workspace-local virtual environments; they do not
# need, and must not receive, root privileges for that workflow.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bubblewrap \
        python3 \
        python3-pip \
        python3-venv \
        tmux \
    && ln -s /usr/bin/python3 /usr/local/bin/python \
    && ln -s /usr/bin/pip3 /usr/local/bin/pip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=codex-installer /usr/local/lib/node_modules/@openai/codex /opt/agentcc/codex
COPY --chmod=755 codex /usr/local/bin/codex

RUN codex --version

# Explicitly retain conventional tool locations for both code-server's `coder`
# shell and the separate `agent` harness user.
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.opencontainers.image.title="AgentCC Codex harness" \
      org.opencontainers.image.description="AgentCC session image with the Codex CLI" \
      org.opencontainers.image.source="https://github.com/nievric/agentcc" \
      org.opencontainers.image.url="https://github.com/nievric/agentcc" \
      org.opencontainers.image.documentation="https://github.com/nievric/agentcc" \
      org.opencontainers.image.vendor="Nievric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
