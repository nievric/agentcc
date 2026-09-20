"""Harness-specific, allowlisted launch configuration.

The browser selects a registered model, never a command line or arbitrary
configuration.  Each harness adapter translates that model into only the
files, environment variables, and argv it explicitly supports.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

from .checkout import checkout_path
from .models import AgentActivity, RegisteredModel, Session
from .providers import ModelProvider, endpoint_from_session, provider_kind
from .usage import ConversationEntry, UsageSnapshot, parse_claude_conversation, parse_claude_usage, parse_codex_conversation, parse_codex_usage, parse_hermes_conversation, parse_hermes_usage, parse_kilo_conversation, parse_kilo_usage


class HarnessConfigurationError(ValueError):
    """A registered model cannot safely be used by the selected harness."""


@dataclass(frozen=True)
class HarnessLaunch:
    entrypoint: str
    environment: dict[str, str]
    files: dict[str, str] = field(default_factory=dict)
    initial_input: str | None = None
    ready_commands: tuple[str, ...] = ()
    ready_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class JournalTelemetryProbe:
    """A bounded local journal source emitted by a harness."""

    directory: str
    filename_pattern: str
    tail_bytes: int = 524288


@dataclass(frozen=True)
class SqliteTelemetryProbe:
    """A harness-owned SQLite store with one current session per container."""

    database_path: str
    diagnostic_log_path: str | None = None
    diagnostic_tail_bytes: int = 131072


@dataclass(frozen=True)
class KiloSqliteTelemetryProbe:
    """Kilo CLI's local SQLite session database."""

    database_path: str


class HarnessAdapter(Protocol):
    """Stable harness contract for launch configuration and usage telemetry."""

    name: str

    def session_image(self, fallback_image: str) -> str: ...
    def requires_executable_tmp(self) -> bool: ...
    def validate_model(self, model: RegisteredModel | None) -> None: ...
    def prepare(self, session: Session, model: RegisteredModel | None, api_key: str | None) -> HarnessLaunch: ...
    def telemetry_probe(self) -> JournalTelemetryProbe | SqliteTelemetryProbe | KiloSqliteTelemetryProbe | None: ...
    def parse_telemetry(self, payload: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None: ...
    def parse_conversation(self, payload: bytes, diagnostics: bytes = b"") -> list[ConversationEntry]: ...
    def classify_terminal_activity(self, output: str, pane_command: str) -> AgentActivity: ...


def _toml_string(value: str) -> str:
    # JSON string encoding is valid TOML basic-string encoding and avoids
    # treating provider/model values as TOML syntax.
    return json.dumps(value)


class CodexHarnessAdapter:
    """Codex CLI adapter with explicit OpenRouter provider support."""

    name = "Codex"

    def session_image(self, fallback_image: str) -> str:
        return os.getenv("AGENTCC_CODEX_SESSION_IMAGE", fallback_image)

    def requires_executable_tmp(self) -> bool:
        return False

    def telemetry_probe(self) -> JournalTelemetryProbe:
        return JournalTelemetryProbe(directory="/home/agent/.codex/sessions", filename_pattern="*.jsonl")

    def parse_telemetry(self, payload: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
        return parse_codex_usage(payload, previous_total_tokens, previous_model_calls)

    def parse_conversation(self, payload: bytes, diagnostics: bytes = b"") -> list[ConversationEntry]:
        return parse_codex_conversation(payload)

    def classify_terminal_activity(self, output: str, pane_command: str) -> AgentActivity:
        """Infer only stable, visible Codex TUI states from its detached pane.

        Codex does not expose a machine-readable interactive activity API. The
        adapter therefore keeps this heuristic local to Codex and leaves every
        other harness free to use its own signals later.
        """
        if pane_command.strip().lower() != "codex":
            return AgentActivity.WAITING_FOR_INPUT
        tail = output.lower()[-1600:]
        working_markers = ("working", "thinking", "running", "executing", "esc to interrupt", "press esc to interrupt")
        if any(marker in tail for marker in working_markers):
            return AgentActivity.WORKING
        waiting_markers = ("waiting for your input", "awaiting your input", "type a message", "what would you like")
        if any(marker in tail for marker in waiting_markers):
            return AgentActivity.WAITING_FOR_INPUT
        meaningful = [line.strip() for line in output.splitlines() if line.strip()]
        if meaningful and meaningful[-1] in {"›", ">"}:
            return AgentActivity.WAITING_FOR_INPUT
        return AgentActivity.UNKNOWN

    def validate_model(self, model: RegisteredModel | None) -> None:
        if model is None:
            return
        try:
            provider_kind(model)
        except ValueError as error:
            raise HarnessConfigurationError(str(error)) from error

    def prepare(self, session: Session, model: RegisteredModel | None, api_key: str | None) -> HarnessLaunch:
        self.validate_model(model)
        environment = {
            "HOME": "/home/agent",
            "TERM": "xterm-256color",
            "AGENTCC_TASK": session.task,
        }
        launcher_path = "/home/agent/.agentcc/run-codex"
        # A default Codex session begins at its interactive sign-in screen.
        # Passing a task as argv there causes Codex to buffer (and sometimes
        # abort on) input before authentication. Registered adapters have
        # non-interactive credentials, so they may safely receive the initial
        # task once configured.
        if model is None:
            # Do not turn a display label into a CLI model identifier. Codex
            # resolves the signed-in account's native default itself.
            launcher = "#!/bin/sh\nexec codex --no-alt-screen\n"
            return HarnessLaunch(entrypoint=launcher_path, environment=environment, files={launcher_path: launcher})

        environment["AGENTCC_MODEL"] = model.model_name
        task_argument = ' "$AGENTCC_TASK"' if session.task.strip() else ""
        launcher = f'#!/bin/sh\nexec codex --no-alt-screen --model "$AGENTCC_MODEL"{task_argument}\n'
        provider = provider_kind(model)
        if provider is ModelProvider.OPENROUTER:
            # The secret itself stays only in exec_create's environment;
            # config.toml contains an auth command, never a key value.
            config = "\n".join(
                [
                    'model_provider = "openrouter"',
                    'wire_api = "responses"',
                    f"model = {_toml_string(model.model_name)}",
                    f"model_reasoning_effort = {_toml_string(model.reasoning_effort.value)}",
                    "",
                    "[model_providers.openrouter]",
                    'name = "openrouter"',
                    f"base_url = {_toml_string(model.endpoint)}",
                    "",
                    "[model_providers.openrouter.auth]",
                    'command = "sh"',
                    "args = [\"-c\", \"printf '%s' \\\"$OPENROUTER_API_KEY\\\"\"]",
                    "",
                ]
            )
            if api_key:
                environment["OPENROUTER_API_KEY"] = api_key
        else:
            # Current Codex uses the OpenAI Responses wire for local OSS
            # providers. The registered local gateway must expose /responses.
            lines = [
                'model_provider = "agentcc_local"',
                'wire_api = "responses"',
                f"model = {_toml_string(model.model_name)}",
                f"model_reasoning_effort = {_toml_string(model.reasoning_effort.value)}",
                "",
                "[model_providers.agentcc_local]",
                'name = "AgentCC local gateway"',
                f"base_url = {_toml_string(endpoint_from_session(model))}",
                'wire_api = "responses"',
            ]
            if api_key:
                lines.append('env_key = "LOCAL_MODEL_API_KEY"')
                environment["LOCAL_MODEL_API_KEY"] = api_key
            config = "\n".join([*lines, ""])
        return HarnessLaunch(
            entrypoint=launcher_path,
            environment=environment,
            files={"/home/agent/.codex/config.toml": config, launcher_path: launcher},
        )


def _yaml_string(value: str) -> str:
    """JSON strings are valid YAML scalars and safely quote user model data."""
    return json.dumps(value)


class HermesHarnessAdapter:
    """Hermes CLI adapter backed by its canonical SQLite session store."""

    name = "Hermes"

    def session_image(self, fallback_image: str) -> str:
        return os.getenv("AGENTCC_HERMES_SESSION_IMAGE", "agentcc-session-hermes:dev")

    def requires_executable_tmp(self) -> bool:
        return False

    def telemetry_probe(self) -> SqliteTelemetryProbe:
        return SqliteTelemetryProbe(
            database_path="/home/agent/.hermes/state.db",
            diagnostic_log_path="/home/agent/.hermes/logs/errors.log",
        )

    def parse_telemetry(self, payload: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
        return parse_hermes_usage(payload, previous_total_tokens, previous_model_calls)

    def parse_conversation(self, payload: bytes, diagnostics: bytes = b"") -> list[ConversationEntry]:
        return parse_hermes_conversation(payload, diagnostics)

    def classify_terminal_activity(self, output: str, pane_command: str) -> AgentActivity:
        if pane_command.strip().lower() != "hermes":
            return AgentActivity.WAITING_FOR_INPUT
        tail = output.lower()[-1600:]
        if any(marker in tail for marker in ("thinking", "working", "running", "generating", "esc to interrupt", "msg=interrupt")):
            return AgentActivity.WORKING
        if any(marker in tail for marker in ("type a message", "waiting for input", "you:", "›", "press enter")):
            return AgentActivity.WAITING_FOR_INPUT
        return AgentActivity.UNKNOWN

    def validate_model(self, model: RegisteredModel | None) -> None:
        if model is None:
            raise HarnessConfigurationError("Hermes requires a registered model; choose an OpenRouter or Local gateway model before launching.")
        try:
            provider_kind(model)
        except ValueError as error:
            raise HarnessConfigurationError(str(error)) from error

    def prepare(self, session: Session, model: RegisteredModel | None, api_key: str | None) -> HarnessLaunch:
        self.validate_model(model)
        assert model is not None  # narrowed by validate_model
        provider = provider_kind(model)
        home = "/home/agent/.hermes"
        workspace = checkout_path(session)
        config = "\n".join(
            [
                "model:",
                f"  provider: {'openrouter' if provider is ModelProvider.OPENROUTER else 'custom'}",
                f"  default: {_yaml_string(model.model_name)}",
                f"  base_url: {_yaml_string(endpoint_from_session(model))}",
                "agent:",
                f"  reasoning_effort: {_yaml_string(model.reasoning_effort.value)}",
                "terminal:",
                "  backend: local",
                f"  cwd: {_yaml_string(workspace)}",
                "sessions:",
                "  auto_prune: false",
                "",
            ]
        )
        launcher_path = "/home/agent/.agentcc/run-hermes"
        # ``chat -q`` is a one-shot in Hermes v0.19, so start its interactive
        # TUI and let the runtime seed the task once the process owns the pane.
        # This keeps the agent alive through browser disconnects.
        launcher = "#!/bin/sh\nexec hermes chat\n"
        environment = {
            "HOME": "/home/agent",
            "HERMES_HOME": home,
            "TERM": "xterm-256color",
            "AGENTCC_MODEL": model.model_name,
            "AGENTCC_TASK": session.task,
        }
        if api_key and provider is ModelProvider.OPENROUTER:
            environment["OPENROUTER_API_KEY"] = api_key
        elif api_key:
            environment["OPENAI_API_KEY"] = api_key
        return HarnessLaunch(
            entrypoint=launcher_path,
            environment=environment,
            files={f"{home}/config.yaml": config, launcher_path: launcher},
            initial_input=session.task,
            # Hermes' console script is Python, so tmux reports ``python3``
            # rather than the console-script name on this image.
            ready_commands=("hermes", "python", "python3"),
            ready_markers=("Welcome to Hermes Agent! Type your message",),
        )


class ClaudeCodeHarnessAdapter:
    """Claude Code adapter for OpenRouter or an Anthropic-compatible gateway."""

    name = "Claude Code"

    def session_image(self, fallback_image: str) -> str:
        return os.getenv("AGENTCC_CLAUDE_CODE_SESSION_IMAGE", "agentcc-session-claude-code:dev")

    def requires_executable_tmp(self) -> bool:
        return False

    def telemetry_probe(self) -> JournalTelemetryProbe:
        # Claude Code stores one JSONL transcript per session below a path
        # derived from the workspace. The generic journal reader recursively
        # selects the newest file for this isolated container.
        return JournalTelemetryProbe(directory="/home/agent/.claude/projects", filename_pattern="*.jsonl")

    def parse_telemetry(self, payload: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
        return parse_claude_usage(payload, previous_total_tokens, previous_model_calls)

    def parse_conversation(self, payload: bytes, diagnostics: bytes = b"") -> list[ConversationEntry]:
        return parse_claude_conversation(payload)

    def classify_terminal_activity(self, output: str, pane_command: str) -> AgentActivity:
        command = pane_command.strip().lower()
        if command not in {"claude", "claude.exe"}:
            return AgentActivity.WAITING_FOR_INPUT
        tail = output.lower()[-1600:]
        if any(marker in tail for marker in ("thinking", "working", "esc to interrupt", "generating", "processing")):
            return AgentActivity.WORKING
        if any(marker in tail for marker in ("type your message", "try \"", "press enter", "what can i help", "›")):
            return AgentActivity.WAITING_FOR_INPUT
        return AgentActivity.UNKNOWN

    def validate_model(self, model: RegisteredModel | None) -> None:
        # No model means Claude Code's own default/authentication flow. This
        # keeps the launcher useful for a native Claude subscription or an
        # operator-provisioned Claude configuration in the session image.
        if model is None:
            return
        try:
            provider_kind(model)
        except ValueError as error:
            raise HarnessConfigurationError(str(error)) from error

    @staticmethod
    def _anthropic_endpoint(endpoint: str) -> str:
        """Translate OpenRouter's OpenAI ``/api/v1`` registration endpoint.

        Claude Code uses OpenRouter's Anthropic-compatible API skin at
        ``/api`` instead. Custom gateway endpoints are otherwise preserved.
        """
        normalized = endpoint.rstrip("/")
        return normalized[:-3] if normalized.endswith("/v1") else normalized

    def prepare(self, session: Session, model: RegisteredModel | None, api_key: str | None) -> HarnessLaunch:
        self.validate_model(model)
        launcher_path = "/home/agent/.agentcc/run-claude-code"
        default_environment = {
            "HOME": "/home/agent",
            "TERM": "xterm-256color",
            "AGENTCC_TASK": session.task,
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        if model is None:
            launcher = "#!/bin/sh\nexec claude --dangerously-skip-permissions \"$AGENTCC_TASK\"\n"
            return HarnessLaunch(entrypoint=launcher_path, environment=default_environment, files={launcher_path: launcher})
        assert model is not None  # narrowed by validate_model
        provider = provider_kind(model)
        if provider is ModelProvider.OPENROUTER and not api_key:
            raise HarnessConfigurationError("Claude Code requires an API key when an OpenRouter model is selected.")
        selected_model = model.model_name
        launcher = "#!/bin/sh\nexec claude --dangerously-skip-permissions --model \"$AGENTCC_MODEL\" \"$AGENTCC_TASK\"\n"
        environment = {**default_environment, "AGENTCC_MODEL": selected_model}
        if provider is ModelProvider.OPENROUTER:
            environment.update({
            # OpenRouter documents this exact Anthropic-compatible setup for
            # Claude Code. Keep the secret process-scoped; nothing written to
            # the isolated container includes the API key.
            "OPENROUTER_API_KEY": api_key,
            "ANTHROPIC_BASE_URL": self._anthropic_endpoint(model.endpoint),
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK": "1",
            "ANTHROPIC_CUSTOM_HEADERS": f"Authorization: Bearer {api_key}",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": selected_model,
            # Claude Code has named model roles. Point each one at the model
            # explicitly selected for this AgentCC session, including agents
            # it may delegate internally.
            "ANTHROPIC_DEFAULT_OPUS_MODEL": selected_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": selected_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": selected_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": selected_model,
            })
        else:
            # Claude Code uses Anthropic Messages. A local registry endpoint
            # therefore needs to be an Anthropic-compatible gateway (for
            # example a local LiteLLM gateway), not a raw OpenAI-only server.
            environment.update({
                "ANTHROPIC_BASE_URL": endpoint_from_session(model),
                "ANTHROPIC_AUTH_TOKEN": api_key or "",
                "ANTHROPIC_API_KEY": "",
                "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK": "1",
                "ANTHROPIC_CUSTOM_MODEL_OPTION": selected_model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": selected_model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": selected_model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": selected_model,
                "CLAUDE_CODE_SUBAGENT_MODEL": selected_model,
            })
            if api_key:
                environment["ANTHROPIC_CUSTOM_HEADERS"] = f"Authorization: Bearer {api_key}"
        # Persist only the non-secret preferences from the supplied settings
        # shape. The environment is passed to the protected harness exec,
        # rather than saved where code-server could read the credential.
        settings = json.dumps({"model": selected_model, "theme": "dark"}, indent=2) + "\n"
        return HarnessLaunch(
            entrypoint=launcher_path,
            environment=environment,
            files={"/home/agent/.claude/settings.json": settings, launcher_path: launcher},
        )


class KiloCodeHarnessAdapter:
    """Kilo CLI adapter using its trusted OpenRouter configuration shape."""

    name = "Kilo Code"

    def session_image(self, fallback_image: str) -> str:
        return os.getenv("AGENTCC_KILOCODE_SESSION_IMAGE", "agentcc-session-kilocode:dev")

    def requires_executable_tmp(self) -> bool:
        # OpenTUI extracts a native renderer into TMPDIR. Its shared object
        # must be executable, unlike the other current harnesses' temp files.
        return True

    def telemetry_probe(self) -> KiloSqliteTelemetryProbe:
        return KiloSqliteTelemetryProbe(database_path="/home/agent/.local/share/kilo/kilo.db")

    def parse_telemetry(self, payload: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
        return parse_kilo_usage(payload, previous_total_tokens, previous_model_calls)

    def parse_conversation(self, payload: bytes, diagnostics: bytes = b"") -> list[ConversationEntry]:
        return parse_kilo_conversation(payload)

    def classify_terminal_activity(self, output: str, pane_command: str) -> AgentActivity:
        if pane_command.strip().lower() != "kilo":
            return AgentActivity.WAITING_FOR_INPUT
        tail = output.lower()[-1600:]
        if any(marker in tail for marker in ("thinking", "working", "generating", "running", "esc to interrupt")):
            return AgentActivity.WORKING
        if any(marker in tail for marker in ("type a message", "enter to send", "ask anything", "›")):
            return AgentActivity.WAITING_FOR_INPUT
        return AgentActivity.UNKNOWN

    def validate_model(self, model: RegisteredModel | None) -> None:
        if model is None:
            raise HarnessConfigurationError("Kilo Code requires a registered OpenRouter or Local gateway model before launching.")
        try:
            provider_kind(model)
        except ValueError as error:
            raise HarnessConfigurationError(str(error)) from error

    def prepare(self, session: Session, model: RegisteredModel | None, api_key: str | None) -> HarnessLaunch:
        self.validate_model(model)
        assert model is not None
        provider = provider_kind(model)
        if provider is ModelProvider.OPENROUTER and not api_key:
            raise HarnessConfigurationError("Kilo Code requires an API key for its selected OpenRouter model.")
        if provider is ModelProvider.OPENROUTER:
            provider_model = model.model_name.removeprefix("openrouter/")
            selected_model = f"openrouter/{provider_model}"
            config = {
                "$schema": "https://app.kilo.ai/config.json",
                "model": selected_model,
                "provider": {"openrouter": {"env": ["OPENROUTER_API_KEY"]}},
            }
        else:
            selected_model = f"agentcc-local/{model.model_name}"
            options: dict[str, str] = {"baseURL": endpoint_from_session(model)}
            if api_key:
                options["apiKey"] = api_key
            config = {
                "$schema": "https://app.kilo.ai/config.json",
                "model": selected_model,
                "provider": {
                    "agentcc-local": {
                        "name": "AgentCC local gateway",
                        "api": "openai-compatible",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": options,
                        "models": {model.model_name: {"name": model.display_name, "id": model.model_name, "tool_call": True}},
                    }
                },
            }
        launcher_path = "/home/agent/.agentcc/run-kilo"
        # Do not `exec` Kilo here.  A command-line/configuration error must
        # remain visible in the one durable tmux pane rather than destroying
        # the pane before a browser can reconnect to inspect it.
        launcher = (
            "#!/bin/sh\n"
            "kilo run --interactive --auto --model \"$AGENTCC_MODEL\" \"$AGENTCC_TASK\"\n"
            "status=$?\n"
            "printf '\\nKilo Code exited with status %s. The diagnostic above remains available in this terminal.\\n' \"$status\"\n"
            "exec /bin/bash --noprofile --norc -i\n"
        )
        environment = {
            "HOME": "/home/agent",
            "TERM": "xterm-256color",
            "XDG_DATA_HOME": "/home/agent/.local/share",
            "XDG_CONFIG_HOME": "/home/agent/.config",
            "XDG_STATE_HOME": "/home/agent/.local/state",
            "XDG_CACHE_HOME": "/home/agent/.cache",
            "KILO_CONFIG_CONTENT": json.dumps(config, separators=(",", ":")),
            "KILO_DISABLE_PROJECT_CONFIG": "1",
            "AGENTCC_MODEL": selected_model,
            "AGENTCC_TASK": session.task,
        }
        if provider is ModelProvider.OPENROUTER:
            environment["OPENROUTER_API_KEY"] = api_key or ""
        return HarnessLaunch(
            entrypoint=launcher_path,
            environment=environment,
            files={launcher_path: launcher},
        )


class HarnessRegistry:
    """Static adapter registry; new harnesses must be code-reviewed additions."""

    def __init__(self) -> None:
        self._adapters = {"codex": CodexHarnessAdapter(), "hermes": HermesHarnessAdapter(), "claude code": ClaudeCodeHarnessAdapter(), "kilo code": KiloCodeHarnessAdapter()}

    def for_name(self, harness: str) -> HarnessAdapter:
        adapter = self._adapters.get(harness.strip().lower())
        if adapter is None:
            raise HarnessConfigurationError(f"Harness {harness!r} is not installed")
        return adapter


harnesses = HarnessRegistry()
