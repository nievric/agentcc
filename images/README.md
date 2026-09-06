# Session image hierarchy

```text
agentcc-session-base
├── agentcc-session-codex
├── agentcc-session-hermes
├── agentcc-session-claude-code
└── agentcc-session-kilocode
```

`base` contains the browser IDE and common Unix developer tools. Each harness
receives its own derived image (`harness-codex`, `harness-hermes`,
`harness-claude-code`, `harness-kilocode`, and future
harnesses). Template images can derive from a harness image to add a project
toolchain without bloating unrelated sessions.

## Public Docker Hub repositories

Use the `nievric` Docker Hub namespace, with one repository per independently
selectable image:

| Purpose | Public repository |
| --- | --- |
| Control-plane API | `nievric/agentcc-api` |
| Web UI | `nievric/agentcc-web` |
| Common session layer | `nievric/agentcc-session-base` |
| Codex session | `nievric/agentcc-session-codex` |
| Hermes session | `nievric/agentcc-session-hermes` |
| Claude Code session | `nievric/agentcc-session-claude-code` |
| Kilo Code session | `nievric/agentcc-session-kilo-code` |

Use an immutable release tag such as `0.1.0` and also publish the major/minor
compatibility tag (`0.1`) only after that release is verified. Reserve `latest`
for the most recent stable release; do not publish development builds as
`latest` or `dev`. Keep every image in a release on the same AgentCC tag so a
control plane cannot accidentally launch an incompatible harness image.

The local development names (`agentcc-session-*:dev`) deliberately remain
unqualified and are not intended as public image names. Before publishing,
replace tagged upstream base images with digest-pinned references, attach OCI
source/version/revision/license labels, generate an SBOM, and run a
vulnerability scan. Never pass API keys, vault keys, or workspace data as
build arguments or build-context files.

See [the public image release guide](../docs/PUBLISHING_IMAGES.md) for the
ordered build commands and release checks. The project source is licensed
under Apache-2.0; `NOTICE` explains the additional attribution work required
for the third-party components included in a container release.
