# AgentCC Hermes harness image

`agentcc-session-hermes` derives from the common code-server image and adds
the pinned Hermes interactive CLI, Python 3.13, and tmux. It deliberately uses
Hermes' `cli` dependency set only: web automation, messaging gateways, and
other optional extras do not inflate ordinary coding sessions.

The AgentCC Hermes adapter writes a non-secret `config.yaml` below
`/home/agent/.hermes`, sets `HERMES_HOME` for the detached CLI process, and
starts interactive `hermes chat` in a persistent tmux session and seeds the
launch task only after Hermes owns the pane. The adapter configures a
selected OpenRouter model, endpoint, reasoning effort, and local workspace
terminal backend. Its OpenRouter key is provided only in the launch process
environment; it is never written into `config.yaml` or exposed to code-server.

Hermes' current transcript and usage source is
`/home/agent/.hermes/state.db`, not the legacy JSONL files. AgentCC reads that
database through a read-only in-container SQLite connection and reads only
error-level lines from Hermes' durable `logs/errors.log` for the interaction
timeline.

Build the base image first, then build this image from the repository root:

```sh
docker build -f images/harness-hermes/Dockerfile -t agentcc-session-hermes:dev .
```
