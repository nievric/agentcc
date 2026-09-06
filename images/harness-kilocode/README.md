# AgentCC Kilo Code harness image

`agentcc-session-kilocode` adds the pinned Kilo Code CLI, tmux, and Python to
the common code-server image. npm is used only in a discarded build stage; the
final image uses the Node runtime already bundled with code-server.

The AgentCC adapter currently supports registered **OpenRouter** models. It
passes the key only to the authorized harness process and supplies a trusted,
non-secret `KILO_CONFIG_CONTENT` provider configuration. Kilo's local SQLite
database provides its durable transcript, token, and native cost data for the
AgentCC session log and telemetry views.

Build after the base image:

```sh
docker build \
  --build-arg BASE_IMAGE=agentcc-session-base:dev \
  --build-arg KILO_VERSION=7.5.16 \
  --tag agentcc-session-kilocode:dev \
  -f images/harness-kilocode/Dockerfile .
```
