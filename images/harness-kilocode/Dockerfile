# Kilo Code is installed in a disposable Node build stage. The final session
# image reuses code-server's bundled Node runtime through a small fixed wrapper.
ARG BASE_IMAGE=agentcc-session-base:dev
ARG KILO_VERSION=7.5.16
ARG VERSION=dev
ARG REVISION=unknown

# Pinned index digest for node:22-bookworm-slim, resolved for the 0.1.0 release.
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS kilo-installer
ARG KILO_VERSION

RUN npm install --global --omit=dev --no-audit --no-fund "@kilocode/cli@${KILO_VERSION}" \
    && npm cache clean --force \
    && rm -rf /root/.npm

FROM ${BASE_IMAGE}

ARG VERSION
ARG REVISION

USER root

# Kilo's one interactive TUI lives in tmux. Python is included for workspace
# projects, matching the other agent session images without giving the session
# users sudo access.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        tmux \
    && ln -s /usr/bin/python3 /usr/local/bin/python \
    && ln -s /usr/bin/pip3 /usr/local/bin/pip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=kilo-installer /usr/local/lib/node_modules/@kilocode/cli /opt/agentcc/kilocode
COPY --chmod=755 images/harness-kilocode/kilo /usr/local/bin/kilo

# Kilo initialises its config directory even for `--version`.  Keep that
# build-time state out of code-server's home; the runtime entrypoint later
# starts code-server as `coder`, while Kilo itself uses `/home/agent`.
RUN HOME=/root kilo --version \
    && chown -R coder:coder /home/coder

ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.opencontainers.image.title="AgentCC Kilo Code harness" \
      org.opencontainers.image.description="AgentCC session image with the pinned Kilo Code interactive CLI" \
      org.opencontainers.image.source="https://github.com/nievric/agentcc" \
      org.opencontainers.image.url="https://github.com/nievric/agentcc" \
      org.opencontainers.image.documentation="https://github.com/nievric/agentcc" \
      org.opencontainers.image.vendor="Nievric" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
