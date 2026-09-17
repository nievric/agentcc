# Small test fixture: real Git, users, tmux, mounts and production bootstrap;
# code-server and the paid harness are replaced by a local HTTP process.
FROM agentcc-git-helper:dev
USER root
RUN apt-get update && apt-get install -y --no-install-recommends tmux util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 workspace \
    && useradd -m -u 1000 -G workspace coder \
    && useradd -m -u 10001 -g workspace agent \
    && install -d -o coder -g workspace -m 2775 /workspaces/session \
    && install -d -o coder -g coder /opt/code-server/extensions
COPY images/base/entrypoint.sh /usr/local/bin/agentcc-session
COPY backend/tests/docker/code-server /usr/local/bin/code-server
RUN chmod 755 /usr/local/bin/agentcc-session /usr/local/bin/code-server
ENV HOME=/home/coder CODE_SERVER_PORT=8080 WORKSPACE_ROOT=/workspaces
ENTRYPOINT ["/usr/local/bin/agentcc-session"]
