# AgentCC Codex harness image

`agentcc-session-codex` adds the pinned Codex CLI and Python 3 with `venv` and
`pip` to the common base image. Its temporary build stage uses npm, but the final image reuses
code-server's bundled Node runtime rather than carrying another Node/npm
installation. The base entrypoint starts code-server only and prepares direct
managed workspace mount roots for the unprivileged IDE user. The control-plane
session supervisor starts Codex as the unprivileged
`agent` user when an authorized user launches an agent session.

Build after the base image:

```sh
docker build \
  --build-arg BASE_IMAGE=agentcc-session-base:dev \
  --build-arg CODEX_VERSION=0.153.4 \
  --tag agentcc-session-codex:dev .
```

Do not provide provider credentials at build time or when starting the IDE.
The control plane supplies a scoped secret only to the short-lived harness
process. It must launch Codex with `HOME=/home/agent` (rather than inheriting
code-server's `HOME=/home/coder`) and `/workspaces/session` as its working
directory.

## User-installed tools and libraries

Both the VS Code terminal (`coder`) and the Codex harness (`agent`) can invoke
`python` and `python3`. Install Python libraries into the durable workspace,
not into the image's system Python:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The workspace volume is shared by the session's editor and harness, so the
project virtual environment remains available while the workspace exists.
The image intentionally does not grant `sudo` to either user. Giving an agent
unrestricted root access would let it inspect active harness credentials and
would weaken the session isolation boundary. Add repeatable OS-level tools to
a derived, pinned image; a future package-install feature should be a
control-plane API with a reviewed allowlist rather than shell `sudo`.
