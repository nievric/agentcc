# AgentCC base session image

`agentcc-session-base` is the common browser-workspace layer. It contains
code-server, Git/SSH, ripgrep, tini, and the unprivileged `coder`/`agent`
runtime users. It contains no harness, provider credentials, compilers, or
language SDKs.

Build from this directory:

```sh
docker build \
  --build-arg CODE_SERVER_VERSION=4.121.0 \
  --tag agentcc-session-base:dev .
```

Do not publish port 8080 in production. Session containers join a private
network and the AgentCC workspace gateway is their only browser-facing route.
