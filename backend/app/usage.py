"""Normalize non-secret usage and transcript data from harness-native stores."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class UsageSnapshot:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    total_tokens: int
    observed_model_calls: int
    estimated_cost_usd: float | None = None


@dataclass(frozen=True)
class ConversationEntry:
    role: str
    text: str
    occurred_at: str


def parse_codex_conversation(journal_tail: bytes) -> list[ConversationEntry]:
    """Extract user/assistant text from Codex journal events, never tool traces.

    Codex has used both ``response_item`` messages and completed protocol
    items for assistant replies.  The latter is the format emitted by recent
    interactive CLI builds, so deliberately handle both forms here.
    """

    def text_from(value: object) -> str:
        """Return visible message text from a Codex content value only."""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "\n".join(part for item in value if (part := text_from(item)))
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                return text.strip()
            # These keys are used by known message protocol versions.  Do not
            # recurse over arbitrary dictionaries: tool call arguments and
            # tool output must never become part of the conversation view.
            for key in ("content", "message", "output_text"):
                if key in value and (result := text_from(value[key])):
                    return result
        return ""

    def occurred_at(record: dict, payload: dict) -> str:
        timestamp = record.get("timestamp") or payload.get("completed_at") or payload.get("started_at")
        if timestamp:
            return str(timestamp)
        completed_at_ms = payload.get("completed_at_ms")
        if isinstance(completed_at_ms, (int, float)):
            return datetime.fromtimestamp(completed_at_ms / 1000, timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()

    entries: list[ConversationEntry] = []
    for raw_line in journal_tail.decode("utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(raw_line); payload = record.get("payload") or {}
            event_time = occurred_at(record, payload)
            role, text = "", ""
            if record.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
                role = "user" if payload["type"] == "user_message" else "assistant"
                text = text_from(payload.get("message"))
            elif record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
                role = str(payload["role"])
                text = text_from(payload.get("content"))
            elif record.get("type") == "event_msg" and payload.get("type") == "item_completed":
                item = payload.get("item") or {}
                item_type = str(item.get("type") or "").replace("_", "").lower()
                if item_type == "agentmessage":
                    role, text = "assistant", text_from(item.get("content") or item.get("message"))
            elif record.get("type") == "event_msg" and payload.get("type") == "task_complete":
                # Some CLI versions retain the final reply solely on this
                # completion event rather than as a response_item.
                error = text_from(payload.get("error"))
                if error:
                    # Provider failures (for example an exhausted retry budget
                    # after a 429) are part of the user-visible interaction,
                    # not terminal-only diagnostics. Codex supplies its safe
                    # message here without requiring us to parse ANSI output.
                    role, text = "system", error
                else:
                    role, text = "assistant", text_from(payload.get("last_agent_message"))
            elif record.get("type") == "event_msg" and payload.get("type") == "turn_aborted":
                reason = text_from(payload.get("reason"))
                if reason:
                    role, text = "system", f"Agent turn aborted: {reason}"
            if role and text.strip():
                entry = ConversationEntry(role=role, text=text.strip(), occurred_at=event_time)
                # A final task_complete event can repeat the immediately
                # preceding assistant response.  Keep one visible reply.
                if not entries or (entries[-1].role, entries[-1].text) != (entry.role, entry.text):
                    entries.append(entry)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return entries


def parse_codex_usage(journal_tail: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
    """Read token_count events without retaining prompts, responses, or secrets."""
    latest: dict[str, int] | None = None
    observed_totals: set[int] = set()
    for raw_line in journal_tail.decode("utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(raw_line)
            payload = record.get("payload", {})
            if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            usage = (payload.get("info") or {}).get("total_token_usage") or {}
            total = int(usage.get("total_tokens") or 0)
            if total <= 0:
                continue
            latest = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
                "reasoning_tokens": int(usage.get("reasoning_output_tokens") or 0),
                "total_tokens": total,
            }
            if total > previous_total_tokens:
                observed_totals.add(total)
        except (TypeError, ValueError, json.JSONDecodeError):
            # A tail can begin or end in a partial JSONL record.
            continue
    if latest is None:
        return None
    return UsageSnapshot(**latest, observed_model_calls=len(observed_totals))


def _claude_text(value: object) -> str:
    """Extract only visible Claude Code text blocks, never tool payloads."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") not in {None, "text"}:
                continue
            if text := _claude_text(item):
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text.strip()
        for key in ("content", "message", "error"):
            if key in value and (text := _claude_text(value[key])):
                return text
    return ""


def _claude_occurred_at(record: dict[str, object]) -> str:
    timestamp = record.get("timestamp")
    if timestamp:
        return str(timestamp)
    return datetime.now(timezone.utc).isoformat()


def parse_claude_conversation(journal_tail: bytes) -> list[ConversationEntry]:
    """Read Claude Code's durable JSONL transcript without exposing tools.

    Interactive Claude Code writes user and assistant messages to a session
    JSONL beneath ``~/.claude/projects``. Error records are included as system
    messages because provider failures are useful in the same timeline.
    """
    entries: list[ConversationEntry] = []
    for raw_line in journal_tail.decode("utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(raw_line)
            if not isinstance(record, dict):
                continue
            record_type = str(record.get("type") or "").lower()
            message = record.get("message")
            role, text = "", ""
            if record_type in {"user", "assistant"}:
                role = record_type
                text = _claude_text(message)
            elif record_type in {"error", "system"}:
                role = "system"
                text = _claude_text(record.get("error") or message or record.get("content"))
            if role and text:
                entry = ConversationEntry(role=role, text=text, occurred_at=_claude_occurred_at(record))
                if not entries or (entries[-1].role, entries[-1].text) != (entry.role, entry.text):
                    entries.append(entry)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return entries


def parse_claude_usage(journal_tail: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
    """Sum per-request usage records from Claude Code's session JSONL."""
    input_tokens = output_tokens = cached_input_tokens = 0
    observed_calls = 0
    for raw_line in journal_tail.decode("utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(raw_line)
            if not isinstance(record, dict) or str(record.get("type") or "").lower() != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            usage = message["usage"]

            def count(key: str) -> int:
                try:
                    return max(0, int(usage.get(key) or 0))
                except (TypeError, ValueError):
                    return 0

            # Anthropic reports cache creation separately. It is not a cache
            # hit, so include it in normal input for the registry's three-rate
            # price model; cache reads use the dedicated cached-input rate.
            cache_read = count("cache_read_input_tokens")
            input_tokens += count("input_tokens") + count("cache_creation_input_tokens") + cache_read
            cached_input_tokens += cache_read
            output_tokens += count("output_tokens")
            observed_calls += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    total_tokens = input_tokens + output_tokens
    if total_tokens <= 0:
        return None
    return UsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=0,
        total_tokens=total_tokens,
        observed_model_calls=max(0, observed_calls - previous_model_calls),
    )


def _kilo_state(payload: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(payload.decode("utf-8", errors="ignore"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _kilo_time(value: object) -> str:
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).isoformat()


def parse_kilo_conversation(payload: bytes) -> list[ConversationEntry]:
    """Read visible text and error parts from Kilo's SQLite export."""
    state = _kilo_state(payload) or {}
    entries: list[ConversationEntry] = []
    for part in state.get("parts", []):
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            continue
        data = part["data"]
        message = part.get("message") if isinstance(part.get("message"), dict) else {}
        part_type = str(data.get("type") or "")
        role = str(message.get("role") or "").lower()
        if part_type == "error":
            role = "system"
            text = _claude_text(data.get("error") or data.get("message"))
        elif part_type == "text" and role in {"user", "assistant"}:
            text = _claude_text(data.get("text"))
        else:
            continue
        if not text:
            continue
        entry = ConversationEntry(role=role, text=text, occurred_at=_kilo_time(part.get("time_created")))
        if not entries or (entries[-1].role, entries[-1].text) != (entry.role, entry.text):
            entries.append(entry)
    return entries


def parse_kilo_usage(payload: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
    """Sum Kilo's native step-finish token and cost records."""
    state = _kilo_state(payload) or {}
    input_tokens = output_tokens = cached_input_tokens = reasoning_tokens = 0
    total_tokens = 0
    model_calls = 0
    cost = 0.0
    cost_available = False
    for part in state.get("parts", []):
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            continue
        data = part["data"]
        if data.get("type") != "step-finish" or not isinstance(data.get("tokens"), dict):
            continue
        tokens = data["tokens"]

        def count(value: object) -> int:
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        cache_read = count(cache.get("read"))
        cache_write = count(cache.get("write"))
        # Normalize to AgentCC's invariant: input includes cached reads, while
        # cache writes remain normal input because the registry has no fourth
        # cache-creation price field.
        input_tokens += count(tokens.get("input")) + cache_read + cache_write
        cached_input_tokens += cache_read
        output_tokens += count(tokens.get("output"))
        reasoning_tokens += count(tokens.get("reasoning"))
        total_tokens += count(tokens.get("total")) or count(tokens.get("input")) + cache_read + cache_write + count(tokens.get("output")) + count(tokens.get("reasoning"))
        model_calls += 1
        try:
            native_cost = float(data.get("cost"))
            if native_cost >= 0:
                cost += native_cost
                cost_available = True
        except (TypeError, ValueError):
            pass
    if total_tokens <= 0:
        return None
    return UsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        observed_model_calls=max(0, model_calls - previous_model_calls),
        estimated_cost_usd=cost if cost_available else None,
    )


def _hermes_text(value: object) -> str:
    """Extract displayable Hermes message content without exposing tool traces."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                return _hermes_text(json.loads(text))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return text
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _hermes_text(item)))
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output_text"):
            if key in value and (text := _hermes_text(value[key])):
                return text
    return ""


def _hermes_state(payload: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(payload.decode("utf-8", errors="ignore"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _hermes_occurred_at(value: object) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except ValueError:
            return value
    return datetime.now(timezone.utc).isoformat()


def parse_hermes_conversation(state_payload: bytes, diagnostics: bytes = b"") -> list[ConversationEntry]:
    """Read Hermes' canonical SQLite message export and its error log.

    Hermes stores CLI messages in ``state.db`` rather than the historical
    JSONL files.  The runtime exports only the newest session's safe message
    fields; this parser intentionally ignores tool calls and tool results.
    """
    state = _hermes_state(state_payload) or {}
    messages = state.get("messages")
    entries: list[ConversationEntry] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").lower()
            if role not in {"user", "assistant", "system"}:
                continue
            text = _hermes_text(message.get("content"))
            if not text:
                continue
            timestamp = message.get("created_at")
            if timestamp is None:
                timestamp = message.get("timestamp")
            if timestamp is None:
                timestamp = message.get("occurred_at")
            occurred_at = _hermes_occurred_at(timestamp)
            entry = ConversationEntry(role=role, text=text, occurred_at=occurred_at)
            if not entries or (entries[-1].role, entries[-1].text) != (entry.role, entry.text):
                entries.append(entry)

    # Hermes' errors.log is a separate, durable diagnostic stream. Keep only
    # error-level lines in the conversation timeline, where provider failures
    # such as a 429 are actionable to the user without exposing debug traces.
    for line in diagnostics.decode("utf-8", errors="ignore").splitlines():
        normalized = line.strip()
        if not normalized or not any(marker in normalized.lower() for marker in ("error", "critical", "exception", "traceback", "rate limit", "429")):
            continue
        occurred_at = datetime.now(timezone.utc).isoformat()
        # Hermes' standard logger uses ``YYYY-MM-DD HH:MM:SS,mmm``. Match the
        # timestamp itself rather than slicing a fixed width, which could
        # accidentally retain the first letters of the log level ("WARNING").
        timestamp = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)", normalized)
        if timestamp:
            occurred_at = timestamp.group(1).replace(" ", "T").replace(",", ".")
        entries.append(ConversationEntry(role="system", text=normalized[-4000:], occurred_at=occurred_at))
    return entries


def parse_hermes_usage(state_payload: bytes, previous_total_tokens: int, previous_model_calls: int = 0) -> UsageSnapshot | None:
    """Normalize the token counters on Hermes' current SQLite session row."""
    state = _hermes_state(state_payload)
    if not state or not isinstance(state.get("session"), dict):
        return None
    session = state["session"]

    def number(*keys: str) -> int:
        for key in keys:
            value = session.get(key)
            try:
                if value is not None:
                    return max(0, int(value))
            except (TypeError, ValueError):
                continue
        return 0

    input_tokens = number("input_tokens", "prompt_tokens")
    output_tokens = number("output_tokens", "completion_tokens")
    cached_input_tokens = number("cache_read_tokens", "cached_input_tokens")
    reasoning_tokens = number("reasoning_tokens", "reasoning_output_tokens")
    total_tokens = number("total_tokens") or input_tokens + output_tokens + cached_input_tokens + reasoning_tokens
    if total_tokens <= 0:
        return None
    api_call_count = number("api_call_count", "api_calls")
    observed_model_calls = max(0, api_call_count - previous_model_calls) if api_call_count else (1 if total_tokens > previous_total_tokens else 0)
    cost = session.get("estimated_cost_usd")
    try:
        estimated_cost_usd = max(0.0, float(cost)) if cost is not None else None
    except (TypeError, ValueError):
        estimated_cost_usd = None
    return UsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        observed_model_calls=observed_model_calls,
        estimated_cost_usd=estimated_cost_usd,
    )
