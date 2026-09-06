# Agent Command Center — System Design

**Status:** proposed MVP architecture  
**Deployment target:** a trusted local machine or private LAN  
**UI reference:** [`mock/`](../mock/), especially the five Agent Command Center screens

## 1. Purpose

Agent Command Center is a local-first control plane for launching and monitoring coding-agent sessions against approved development workspaces. It brings together the operational views represented in the mocks:

- isolated, browser-hosted VS Code workspaces;
- agent fleet and individual session status;
- interactive session output and controls;
- provider/model configuration and encrypted credentials; and
- local user and runtime preferences.

The first release is for a small set of trusted named users on the host machine or a private network. It is not a hosted execution platform, does not execute arbitrary commands submitted from a browser, and does not attempt multi-tenant isolation.

## 2. Goals and non-goals

### MVP goals

- Create an isolated container-backed workspace from an approved repository or template and open it in browser-hosted VS Code.
- Create a managed shared workspace and attach it to selected sessions as an additional VS Code folder.
- Run the configured agent harness beside VS Code in that same session container; observe its output live, and suspend, resume, stop, or restart it.
- Provide a fleet overview with useful session state, token/cost counters when reported by a harness, and host telemetry.
- Store model definitions, provider settings, and secrets locally; never return secret values after creation.
- Persist settings and operational history across restarts.
- Run with a React frontend and Python REST API on a single host, accessible over a configured LAN interface.

### Explicit non-goals for MVP

- Cloud deployment, multi-organization tenancy, SSO/RBAC, and Internet exposure.
- Scheduling across remote worker machines or Kubernetes.
- Arbitrary interactive shell / PTY access from the browser. The only interactive terminal in MVP is an authorized session attached to the Codex harness.
- A generic plugin marketplace or workflow engine.
- Guaranteed token/cost accounting for harnesses that do not expose usage data.

## 3. Product scope mapped to mock screens

| Screen | MVP behavior | Later behavior |
| --- | --- | --- |
| Agent Fleet Sessions Overview | List/filter sessions; launch, stop, suspend, resume; aggregate metrics. | Queues, policies, remote runners, detailed utilization history. |
| Interactive Terminal | Render the authorized Codex session with terminal fidelity; stream output, forward input/resize to that session only, and show status, model, workspace, metrics, and lifecycle controls. | Additional harness-specific terminal/approval protocols. |
| Workspace File Explorer | Create/select a workspace session, attach permitted shared workspaces, and open browser-hosted VS Code. VS Code owns file browsing, editing, Git, extensions, its integrated terminal, and normal file transfer. | Persistent templates, repo import/export, collaboration, and remote Git credential flows. |
| Model Registry & API Vault | CRUD model/provider metadata; add, rotate, delete, and test credentials; masked secret display. | External KMS, provider discovery, organization-scoped policies. |
| Settings & Preferences | Persist terminal display, agent defaults, workspace/storage, and notification settings. | User accounts, profiles, webhook delivery, policy administration. |

## 4. Architecture

The system is deliberately a local control plane with isolated session containers: one browser client, one API/control-plane process, one database, a workspace gateway, and a dedicated container per workspace session. This avoids introducing a queue, broker, or distributed control plane before the product needs one, while preventing different agents from sharing a runtime.

```text
Browser (React command center)                 Browser-hosted VS Code
        | REST/WebSocket                                  ^
        v                                                 | HTTPS + WebSocket
FastAPI control plane -- session/event service -- Workspace gateway
        |                        |                         |
        |                        +--> SQLite + secret store |
        +--> container runtime API                         |
                  |                                       |
                  v                                       |
       dedicated session container <----------------------+
       - code-server (private port only)
       - Codex harness (separate process/user)
       - session workspace volume
       - internal session supervisor
```

### Component responsibilities

| Component | Responsibility |
| --- | --- |
| React SPA | Routing, dense command-center UI, REST queries/mutations, WebSocket subscription, locally cached view state. It never accesses a filesystem or provider key directly. |
| FastAPI control plane | Validates requests, manages identity, starts/stops container sessions, exposes resource-oriented APIs, and publishes an OpenAPI contract. It never serves workspace files directly. |
| Workspace gateway | A same-origin, authenticated HTTP/WebSocket relay. It verifies user/session access, maps `/workspaces/{session_id}/ide/` to the container's private code-server endpoint, and relays VS Code WebSockets without exposing any container port to the LAN. |
| Application services | Implements session authorization/state transitions, runtime provisioning, settings, credentials, and telemetry aggregation. Keeps routers thin. |
| Harness runner adapters | Starts/stops a known installed agent integration inside a session container using an explicit command template; captures output and normalizes lifecycle/usage events. |
| Event hub | Assigns monotonically increasing event IDs, persists useful events, and fans out session/fleet/workspace changes over WebSocket. |
| SQLite | Durable local source of truth for metadata, configuration, audit entries, and bounded session event history. WAL mode is enabled for concurrent reads. |
| Secret store | Encrypts credential material at rest. The database stores only a secret reference, provider, key label, timestamps, and a non-sensitive suffix. |
| Session container | A disposable, dedicated runtime containing code-server, the workspace volume, and the agent harness. It is the only location where project file operations occur. |
| Session supervisor | A small process in the session container that starts code-server and the harness, reports lifecycle/output over the internal control channel, and owns the harness PTY. It is not publicly reachable. |

### Session lifecycle and traffic

1. The user selects a template/repository and creates a workspace session.
2. The control plane creates a unique volume, seeds it, then starts one container on a private network. Any permitted shared-workspace volumes are attached at fixed paths such as `/workspaces/shared/design-assets`; the generated VS Code multi-root workspace includes the private project and each attachment. The container's supervisor starts code-server on an un-published internal port.
3. The command center obtains an IDE route and navigates to `/workspaces/{session_id}/ide/`. The gateway validates the signed user session and proxies HTTP/WebSocket traffic to that container only.
4. The user launches Codex from the command center. The control plane creates exactly one detached tmux session in the same container and runs Codex inside it. Browser terminal WebSockets attach disposable tmux clients, so disconnecting, refreshing, navigating away, or restarting the control plane does not start or terminate Codex.
5. The command center relays the authorized agent stream to xterm.js and the fleet views. VS Code continues to provide its own file UI and integrated terminal through the IDE route.
6. Stopping or archiving a session stops the container. Its named volume is retained or deleted according to the selected retention policy; it is never reused by another session.

### Shared workspace rules

A shared workspace is a managed named volume, owned by a user and granted to named users; it is not a direct mount of an arbitrary host directory. A grant determines who can attach it, while the attachment determines how it is mounted in a particular session.

- Attachments default to **read-only**. A write grant is required for a read/write mount.
- The default write policy is **one writer at a time**; other attached sessions stay read-only. An owner can explicitly opt into concurrent writers for collaboration, after acknowledging that filesystem volumes do not resolve conflicting edits.
- A shared source-code folder should normally use Git branches/commits for collaboration. Shared read/write volumes are better suited to durable artifacts, datasets, generated assets, or a carefully coordinated handoff.
- Detaching a shared workspace only unmounts it from that session. Deleting a session never deletes a shared volume. Deleting a shared workspace requires owner confirmation and an audit event.
- Code-server receives the same mount mode the control plane authorized; it cannot turn a read-only shared folder into a writable one.

## 5. Technology choices

- **Frontend:** React, TypeScript, Vite, React Router, TanStack Query, and a small state store (Zustand). CSS variables plus component CSS implement the supplied dark technical design tokens; use a chart library only after live telemetry is present.
- **Terminal:** [`@xterm/xterm`](https://xtermjs.org/docs/) v6, wrapped in a React `TerminalPane` component and executed in the user's browser—not in a session container. It renders the authorized Codex PTY exposed by the session supervisor through the control plane. Use `@xterm/addon-fit`, `@xterm/addon-search`, and `@xterm/addon-web-links`; defer the WebGL renderer until profiling demonstrates a need. xterm.js renders a terminal—it does not itself create or secure a shell—so the API retains complete control of which process it connects to.
- **Hosted IDE:** [code-server](https://coder.com/docs/code-server/guide), pinned to a tested image version and launched only inside session containers. It is a browser-hosted Code OSS implementation with self-hosted features such as sub-path support and a built-in port proxy. Its extension marketplace is not Microsoft's marketplace, so production images preinstall/allowlist required Open VSX or approved VSIX extensions. The command center is the identity and routing authority; code-server's own password login is disabled on the private container interface.
- **Runtime:** Docker Engine or a Docker-compatible local OCI runtime for MVP. A `ContainerRuntime` port isolates the service from engine specifics so a cloud worker runtime can replace it later.
- **Backend:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, Uvicorn. A service/repository separation makes eventual migration from SQLite straightforward.
- **Database:** SQLite for MVP, stored under an application data directory outside registered workspaces. Configure WAL, foreign keys, and regular local backups. PostgreSQL is the cloud migration target.
- **Real-time:** native FastAPI WebSockets. They provide the log/telemetry latency the console needs without polling. REST remains the source of truth and supports reconnect recovery through event cursors.
- **Process control:** the control plane calls the container runtime with structured arguments only—never shell interpolation. Inside a session container, the Codex adapter creates a PTY so the authorized Codex session behaves correctly in the terminal; it still uses a structured command builder, bounded output buffers, and process-group cleanup.
- **Packaging:** Docker Compose for reproducible local/LAN installation. In development, run Vite and FastAPI separately; in release, serve static frontend assets behind a reverse proxy or from FastAPI.

## 6. Data model

All IDs are UUIDs. Timestamps are UTC ISO-8601 values. Enum values are validated by the API.

| Entity | Key fields |
| --- | --- |
| `workspace_template` | `id`, `name`, `source_type`, `repository_url`, `base_image`, `enabled` |
| `workspace_session` | `id`, `template_id`, `owner_user_id`, `container_id`, `volume_id`, `ide_state`, `created_at`, `last_opened_at`, `expires_at` |
| `workspace_root_policy` | approved host import roots, configured by the host operator; only used to seed/copy a new session volume, never served to the browser |
| `shared_workspace` | `id`, `name`, `owner_user_id`, `volume_id`, `status`, `created_at`, `retention_policy` |
| `shared_workspace_grant` | `shared_workspace_id`, `user_id`, `permission` (`read` or `write`), `created_at` |
| `shared_workspace_mount` | `shared_workspace_id`, `workspace_session_id`, `mount_path`, `mode` (`ro` or `rw`), `attached_by`, `attached_at` |
| `agent_harness` | `id`, `name`, `adapter_type`, `command_template`, `enabled`, `default_model_id` |
| `agent_session` | `id`, `workspace_session_id`, `harness_id`, `model_id`, `state`, `container_id`, `process_ref`, `started_at`, `ended_at`, `exit_code`, `summary` |
| `session_event` | `id` (monotonic), `session_id`, `type`, `level`, `payload`, `created_at` |
| `usage_sample` | `session_id`, `input_tokens`, `output_tokens`, `estimated_cost`, `tool_calls`, `captured_at` |
| `model` | `id`, `provider`, `model_key`, `display_name`, `context_window`, `enabled`, `credential_id` |
| `credential` | `id`, `provider`, `label`, `secret_ref`, `last_four`, `status`, `last_tested_at`, `rotated_at` |
| `setting` | `key`, `value_json`, `updated_at` |
| `audit_event` | `id`, `action`, `resource_type`, `resource_id`, `actor`, `metadata_json`, `created_at` |

`agent_session.state` is one of `queued`, `starting`, `running`, `suspending`, `suspended`, `stopping`, `completed`, `failed`, or `cancelled`. The service owns all transitions, ensuring the UI cannot create an impossible state.

## 7. API and event contract

All REST endpoints are versioned beneath `/api/v1`. Errors use RFC 9457-style problem responses with a stable machine-readable `code`.

| Area | Main endpoints |
| --- | --- |
| System | `GET /health`, `GET /system/summary`, `GET /telemetry` |
| Workspaces | `GET/POST /workspace-templates`, `GET/POST /workspace-sessions`, `GET /workspace-sessions/{id}`, `POST /workspace-sessions/{id}/start`, `/stop`, `/archive`, `GET /workspace-sessions/{id}/ide-url` |
| Shared workspaces | `GET/POST /shared-workspaces`, `GET/PATCH/DELETE /shared-workspaces/{id}`, `POST /shared-workspaces/{id}/grants`, `POST /workspace-sessions/{id}/shared-workspaces`, `DELETE /workspace-sessions/{id}/shared-workspaces/{shared_workspace_id}` |
| Sessions | `GET/POST /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/suspend`, `/resume`, `/stop`, `/restart`, `GET /sessions/{id}/events` |
| Harnesses | `GET /harnesses`, `POST/PATCH /harnesses` (host-admin only in a LAN installation) |
| Models & credentials | `GET/POST/PATCH /models`, `GET/POST /credentials`, `POST /credentials/{id}/test`, `POST /credentials/{id}/rotate`, `DELETE /credentials/{id}` |
| Settings | `GET /settings`, `PATCH /settings` |

`POST /sessions` accepts `{workspace_session_id, harness_id, model_id?, task?}`. The control plane starts the agent inside that session's container. The optional task only reaches the adapter-defined safe input channel; it never becomes part of a shell command.

WebSocket clients connect to `/api/v1/ws/events?after=<event-id>`. The server emits envelopes such as:

```json
{
  "id": 1042,
  "type": "session.output",
  "occurred_at": "2026-09-06T18:22:10Z",
  "data": {"session_id": "…", "stream": "stdout", "text": "…"}
}
```

Core event types are `session.created`, `session.state_changed`, `session.output`, `session.usage`, `workspace.runtime_changed`, `credential.tested`, and `telemetry.updated`. On reconnect, the client refetches affected REST resources if its cursor is outside the retained event window.

### Interactive terminal protocol

The React `TerminalPane` is instantiated per selected running Codex session. It reads terminal output from the event stream, and uses a dedicated authenticated WebSocket, `/api/v1/sessions/{id}/terminal`, for terminal input and dimensions:

```json
{"type":"input","data":"explain this error\\r"}
{"type":"resize","cols":120,"rows":38}
```

The backend verifies the requesting named local user can access the session, verifies the session belongs to the `codex` adapter and is in an input-capable state, then forwards input/resize bytes only to that session's PTY. It rejects control sequences that create a new session, user-supplied executable/working-directory changes, and any request for a standalone shell. Output is also persisted as sanitized, bounded `session.output` events for reconnection and the non-terminal activity log.

## 8. Workspace and container safety

The local-network scope still requires strong guardrails because the application can launch processes and reveal source code.

- The command center does not implement workspace file CRUD. A template creates a fresh dedicated named volume; a host-directory import is copied into that volume from an approved environment-configured root. Containers never bind-mount a user-selected host path, so sessions cannot interfere through a shared working tree.
- Users upload files by drag-and-drop or file selection in the VS Code Explorer, targeting either their private session folder or a shared folder for which they hold an active write mount. Current code-server supports controls to disable browser uploads/downloads; our image enables them for authorized IDE users, enforces gateway request-size limits, and records volume/session context in audit events. The capability is intentionally not duplicated in the command center for MVP.
- Browser downloads should likewise use VS Code for individual files. Add a command-center import/export UI only for requirements that the IDE cannot meet cleanly: audited bulk archives, malware scanning, size/quota workflows, approval gates, or transfers by non-IDE users.
- VS Code binds only to the private container network. The workspace gateway is the sole public path and authorizes every HTTP/WebSocket connection before resolving the session's code-server endpoint. Do not embed it in an iframe for MVP: open the same-origin IDE route as a top-level browser route/window to preserve VS Code keyboard shortcuts and avoid framing-policy issues.
- Codex is the first officially supported harness. Its configuration is host-owned, not user-supplied at launch. The container entrypoint starts code-server and the harness as separate unprivileged users/processes; commands use argument arrays and allowlisted environment variables, never `shell=True`, browser-provided executable paths, or an independent host shell.
- Every session receives a unique container, workspace volume, network identity, process group, CPU/memory/pid limits, output-size limits, and timeout. Containers run rootless where supported, with dropped Linux capabilities, `no-new-privileges`, a read-only root filesystem where compatible, writable tmpfs paths, and no Docker socket or host-device mounts.
- Credentials are write-only at the API: responses expose label, provider, state, and last four characters only. The container supervisor injects a narrowly scoped secret only into the harness process at start—not code-server or VS Code terminals—and removes it when the process exits. Encrypt stored material with an installation key held outside the database; production/cloud migration replaces this with a managed KMS.
- Container egress is disabled by default except for explicitly configured provider/Git endpoints. No container port is published to the host or LAN. The runtime control socket is available only to the control plane service account.
- LAN mode defaults to loopback. Binding a private interface requires an explicit configuration flag, CORS allowlist, HTTPS certificate configuration, and application authentication. Public-interface binding is rejected by default.
- Audit credential changes, workspace changes, launches, and lifecycle actions. Retain bounded session output and provide a local cleanup policy.

## 9. Authentication and deployment stages

### Local named-user mode (MVP default)

Bind to `127.0.0.1`. The installer creates a local administrator and supports additional named local users; credentials are stored with an adaptive password hash and authenticated with signed, secure session cookies. Role checks protect host-level actions (harness configuration, workspace-root policy, and credential administration), and ownership/access checks protect sessions. Development mode may relax this only for Vite’s configured origin.

### Trusted LAN (MVP supported configuration)

Run behind Caddy or another TLS reverse proxy. Bind the API to loopback, expose only the proxy to the LAN, retain named-user password login with signed, secure session cookies, and configure explicit allowed origins. Private-network access is a deployment responsibility, not proof of identity.

### Cloud evolution

Split stateless API/gateway instances from worker execution; move state to PostgreSQL, events to Redis/NATS, logs/artifacts and workspace snapshots to object storage, secrets to KMS/Vault, and authenticate through OIDC. Remote runners register using mutual TLS and have a separate runner protocol. The REST resource model and normalized event envelope stay stable.

## 10. MVP implementation plan

1. **Foundation:** monorepo layout, Compose, FastAPI health endpoint, React app shell, shared API types generated from OpenAPI, design tokens and responsive three-rail layout.
2. **Container workspace MVP:** a pinned code-server image, workspace-template/session/shared-workspace schema, unique volumes, read-only/exclusive-write shared mounts, a Docker runtime adapter, and a same-origin authenticated IDE gateway. Opening a session routes to its hosted VS Code; no file-explorer CRUD API is built.
3. **Agent MVP:** Codex runner adapter in the same session container plus a deterministic demo adapter; session lifecycle state machine; persisted logs/events; fleet screen; xterm.js-based Codex console with WebSocket streaming, input, and resize. VS Code's integrated terminal remains available for workspace use.
4. **Configuration MVP:** named local users; models; write-only credential vault using local encryption; harness-specific configuration adapters; connection-test adapter; extension/image policy; and settings persistence.
5. **Hardening:** authentication, TLS/LAN deployment guide, gateway authorization, container resource/egress policies, audit log, retention, error handling, tests, and backup/restore instructions.

The initial UI should use the demo adapter and fixture telemetry so every mock-derived screen is functional without real provider credentials. Codex is the first real harness integration. Future harnesses are added one adapter at a time, with capabilities explicitly declared (for example, `supports_terminal_input`, `can_suspend`, `reports_usage`, `accepts_task`).

## 11. Repository layout

```text
agentcc/
  frontend/                 # React/Vite application
    src/{api,components,features,styles}/
  backend/
    app/{api,core,db,models,repositories,schemas,services,runners,runtime,gateway}/
    tests/
  images/
    base/                   # code-server + common minimal runtime
    harness-codex/          # base + pinned Codex CLI
  docs/
    SYSTEM_DESIGN.md
  deploy/
    compose.yaml
    Caddyfile
  mock/                     # supplied screen references; not runtime source
```

## 12. Quality attributes and acceptance checks

- The fleet screen becomes usable with a fresh install and no external API key (demo data/harness).
- A running session’s output appears in the terminal view within one second on a local machine, and reconnecting does not silently lose retained output.
- Two concurrently running sessions cannot see or modify each other's workspace volume, process list, or code-server endpoint.
- A shared workspace appears only in sessions to which it has been explicitly attached; a read-only attachment remains read-only in VS Code and for the harness process.
- An authorized user can drag/drop-upload into the private or writable shared folder through VS Code. The same operation is denied for a read-only shared mount.
- A browser cannot reach a session container directly; the gateway rejects an IDE/terminal request unless its authenticated user owns or is granted access to that session.
- Secret values cannot be retrieved through the UI, REST API, logs, or error responses.
- Stopping a session terminates its complete process group and records a terminal state.
- The desktop layout mirrors the supplied dense three-rail visual language; at narrower widths the inspector and navigation become overlays/tabs rather than clipping content.
- API service, gateway authorization, container-runtime argument construction, session state transitions, and runner argument construction have automated tests. End-to-end tests cover session provisioning, hosted IDE WebSocket relay, demo-session output, and credential masking.

## 13. Recorded MVP decisions

1. **First harness:** Codex, with normalized process lifecycle, PTY output/input, and usage telemetry when exposed by the installed Codex version.
2. **Identity:** named local users from the first LAN-capable release; initialize a local administrator during installation.
3. **Workspace source:** a new session receives an isolated volume seeded from a configured template/repository. Optional host-directory imports are limited to environment-configured roots (for example, `AGENTCC_WORKSPACE_ROOTS`) and copied into the volume.
4. **Credentials:** the encrypted credential vault is required in MVP. The container supervisor provides a narrowly scoped decrypted secret only to the harness process at start; no secret is persisted to a workspace, passed to code-server, or returned to the browser.
5. **Hosted IDE:** code-server runs in every session container. The command-center workspace gateway provides the only user-facing route to it; the command center does not implement file CRUD.
6. **Harness configuration:** model registration is provider metadata, while each harness adapter owns the exact runtime configuration it accepts. The Codex adapter writes an agent-owned `~/.codex/config.toml` for supported OpenRouter models and supplies `OPENROUTER_API_KEY` only to that Codex PTY process. Hermes and other harnesses are added only after their configuration and credential-scoping behavior are researched and implemented as separate adapters.
