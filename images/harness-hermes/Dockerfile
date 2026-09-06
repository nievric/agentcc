# Hermes has a large optional feature surface. This image installs only its
# interactive CLI dependency set; browser automation, gateway integrations,
# and messaging adapters remain out of the session image.
ARG BASE_IMAGE=agentcc-session-base:dev
ARG HERMES_REF=v2026.7.20
ARG HERMES_COMMIT=3ef6bbd201263d354fd83ec55b3c306ded2eb72a
ARG UV_VERSION=0.7.12
ARG VERSION=dev
ARG REVISION=unknown

# Pinned index digest for ghcr.io/astral-sh/uv:0.7.12, resolved for the 0.1.0
# release.
FROM ghcr.io/astral-sh/uv:${UV_VERSION}@sha256:4faec156e35a5f345d57804d8858c6ba1cf6352ce5f4bffc11b7fdebdef46a38 AS uv
FROM ${BASE_IMAGE}

ARG HERMES_REF
ARG HERMES_COMMIT
ARG VERSION
ARG REVISION

USER root

# Hermes requires Python 3.11+. Debian trixie supplies Python 3.13, which is
# also the version used by Hermes' official Debian 13 image. Tmux owns the one
# browser-reconnectable process, just as it does for the Codex harness.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-venv \
        tmux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

# Verify the annotated upstream release tag resolves to the recorded commit.
# ``cli`` supplies the TUI without pulling Hermes' browser, messaging, or
# gateway extras. Installing the project without dependencies preserves the
# lockfile-resolved environment.
RUN git clone --depth 1 --branch "${HERMES_REF}" https://github.com/NousResearch/hermes-agent.git /opt/agentcc/hermes \
    && cd /opt/agentcc/hermes \
    && test "$(git rev-parse HEAD)" = "${HERMES_COMMIT}" \
    && uv venv /opt/agentcc/hermes-venv \
    && UV_PROJECT_ENVIRONMENT=/opt/agentcc/hermes-venv uv sync --frozen --no-install-project --extra cli \
    && uv pip install --python /opt/agentcc/hermes-venv/bin/python --no-deps . \
    && rm -rf /root/.cache/uv /opt/agentcc/hermes

ENV PATH=/opt/agentcc/hermes-venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN hermes --version

LABEL org.opencontainers.image.title="AgentCC Hermes harness" \
      org.opencontainers.image.description="AgentCC session image with the pinned Hermes interactive CLI" \
      org.opencontainers.image.source="https://github.com/nievric/agentcc" \
      org.opencontainers.image.url="https://github.com/nievric/agentcc" \
      org.opencontainers.image.documentation="https://github.com/nievric/agentcc" \
      org.opencontainers.image.vendor="Nievric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
