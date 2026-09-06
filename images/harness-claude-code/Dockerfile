# Claude Code is installed in a disposable Node build stage. Its pinned CLI is
# copied into the common code-server image, so the final session image does not
# retain npm's cache or package manager tooling.
ARG BASE_IMAGE=agentcc-session-base:dev
ARG CLAUDE_CODE_VERSION=2.1.266
ARG VERSION=dev
ARG REVISION=unknown

# Pinned index digest for node:22-bookworm-slim, resolved for the 0.1.0 release.
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS claude-installer
ARG CLAUDE_CODE_VERSION

RUN npm install --global --omit=dev --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && rm -rf /root/.npm

FROM ${BASE_IMAGE}

ARG VERSION
ARG REVISION

USER root

# Tmux keeps the one Claude Code TUI alive across browser reconnects. Python is
# exposed for projects opened from the hosted IDE, matching the other agent
# session images without granting either session user sudo access.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        tmux \
    && ln -s /usr/bin/python3 /usr/local/bin/python \
    && ln -s /usr/bin/pip3 /usr/local/bin/pip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=claude-installer /usr/local/lib/node_modules/@anthropic-ai/claude-code /opt/agentcc/claude-code

RUN ln -s /opt/agentcc/claude-code/bin/claude.exe /usr/local/bin/claude \
    && claude --version

ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    DISABLE_AUTOUPDATER=1

LABEL org.opencontainers.image.title="AgentCC Claude Code harness" \
      org.opencontainers.image.description="AgentCC session image with the pinned Claude Code interactive CLI" \
      org.opencontainers.image.source="https://github.com/nievric/agentcc" \
      org.opencontainers.image.url="https://github.com/nievric/agentcc" \
      org.opencontainers.image.documentation="https://github.com/nievric/agentcc" \
      org.opencontainers.image.vendor="Nievric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
