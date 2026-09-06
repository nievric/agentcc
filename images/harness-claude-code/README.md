# AgentCC Claude Code harness image

`agentcc-session-claude-code` adds the pinned Claude Code CLI, Python, and
tmux to the common code-server image. The CLI is installed in a temporary Node
stage; the final image contains only the installed Claude Code package and its
runtime files, not npm's package cache.

The AgentCC Claude Code adapter supports registered **OpenRouter** models. It
starts the interactive CLI in the session-owned tmux server and configures the
process with OpenRouter's Anthropic-compatible endpoint. `ANTHROPIC_AUTH_TOKEN`
is supplied only to that authorized process and is never written to a config
file or exposed to code-server.

Build after the base image:

```sh
docker build \
  --build-arg BASE_IMAGE=agentcc-session-base:dev \
  --build-arg CLAUDE_CODE_VERSION=2.1.266 \
  --tag agentcc-session-claude-code:dev \
  -f images/harness-claude-code/Dockerfile .
```

Claude Code is launched with its documented `--dangerously-skip-permissions`
flag because each AgentCC session already runs in its own restricted,
unpublished container. Do not add privileged Docker capabilities or sudo to
this image; use a derived, pinned image when a project needs additional OS
tools.
