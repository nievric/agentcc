"""SQLite-backed local persistence for the AgentCC MVP."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from .models import AppEvent, ConversationMessage, ModelRegistration, RegisteredModel, Session, SessionAction, SessionLog, SessionLogTail, SessionState, Summary, Telemetry, Workspace, WorkspaceCreate
from .vault import VaultError, vault


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    """Parse durable timestamps without letting one bad log entry break history."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        # Recover the valid timestamp prefix from older Hermes diagnostics,
        # whose stored value accidentally included part of the log level.
        match = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)", str(value))
        if not match:
            return _now()
        try:
            parsed = datetime.fromisoformat(match.group(1).replace(" ", "T").replace(",", "."))
        except ValueError:
            return _now()
    # Older harness records may omit an offset even though their source
    # timestamps are UTC. Make the meaning explicit before sorting or
    # serializing for browser-side timezone conversion.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class AgentCCStore:
    """Durable local metadata. Workspace file assets reside in Docker volumes."""

    def __init__(self) -> None:
        data_dir = Path(os.getenv("AGENTCC_DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = Path(os.getenv("AGENTCC_DATABASE", str(data_dir / "agentcc.db")))
        self.session_log_dir = Path(os.getenv("AGENTCC_SESSION_LOG_DIR", str(data_dir / "session-logs")))
        self.session_log_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    volume_name TEXT NOT NULL UNIQUE,
                    folder_name TEXT NOT NULL,
                    host_path TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    workspace_name TEXT NOT NULL,
                    workspace_folder_name TEXT,
                    harness TEXT NOT NULL,
                    model TEXT NOT NULL,
                    model_id TEXT,
                    state TEXT NOT NULL,
                    task TEXT NOT NULL,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    cost_estimate_available INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    container_id TEXT,
                    volume_name TEXT,
                    harness_started INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    stream TEXT NOT NULL,
                    text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_session_logs_session ON session_logs(session_id, id);
                CREATE TABLE IF NOT EXISTS session_conversation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    fingerprint TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(session_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_session_conversation_session ON session_conversation(session_id, id);
                CREATE TABLE IF NOT EXISTS token_usage_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    tokens INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_token_usage_samples_occurred_at ON token_usage_samples(occurred_at);
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    label TEXT NOT NULL,
                    secret_encrypted TEXT NOT NULL,
                    last_four TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registered_models (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL DEFAULT 'medium',
                    input_cost_per_million REAL,
                    output_cost_per_million REAL,
                    cached_input_cost_per_million REAL,
                    credential_id TEXT NOT NULL REFERENCES credentials(id),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            workspace_columns = {row["name"] for row in connection.execute("PRAGMA table_info(workspaces)")}
            if "host_path" not in workspace_columns:
                connection.execute("ALTER TABLE workspaces ADD COLUMN host_path TEXT")
            if "folder_name" not in workspace_columns:
                connection.execute("ALTER TABLE workspaces ADD COLUMN folder_name TEXT")
                connection.execute("UPDATE workspaces SET folder_name = id WHERE folder_name IS NULL")
            if "deleted_at" not in workspace_columns:
                connection.execute("ALTER TABLE workspaces ADD COLUMN deleted_at TEXT")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
            migrations = {
                "workspace_folder_name": "TEXT",
                "model_id": "TEXT",
                "cost_estimate_available": "INTEGER NOT NULL DEFAULT 0",
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
                "model_calls": "INTEGER NOT NULL DEFAULT 0",
                "ended_at": "TEXT",
                "harness_started": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
            connection.execute("UPDATE sessions SET workspace_folder_name = workspace_id WHERE workspace_folder_name IS NULL")

            model_columns = {row["name"] for row in connection.execute("PRAGMA table_info(registered_models)")}
            if "reasoning_effort" not in model_columns:
                connection.execute("ALTER TABLE registered_models ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'medium'")
            model_migrations = {
                "input_cost_per_million": "REAL",
                "output_cost_per_million": "REAL",
                "cached_input_cost_per_million": "REAL",
            }
            for name, definition in model_migrations.items():
                if name not in model_columns:
                    connection.execute(f"ALTER TABLE registered_models ADD COLUMN {name} {definition}")

            # Sessions completed before `ended_at` was introduced have no exact
            # terminal timestamp. Their last update is the closest durable value.
            connection.execute(
                "UPDATE sessions SET ended_at = updated_at WHERE state = ? AND ended_at IS NULL",
                (SessionState.COMPLETED.value,),
            )

    def _emit(self, connection: sqlite3.Connection, event_type: str, data: dict[str, str | int | float]) -> None:
        import json

        connection.execute(
            "INSERT INTO events(type, occurred_at, data_json) VALUES (?, ?, ?)",
            (event_type, _timestamp(_now()), json.dumps(data, separators=(",", ":"))),
        )
        connection.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 250)")

    def _workspace_from_row(self, row: sqlite3.Row) -> Workspace:
        return Workspace(
            id=UUID(row["id"]), name=row["name"], description=row["description"], status=row["status"],
            attached_sessions=row["attached_sessions"], last_opened=row["last_opened"], volume_name=row["volume_name"],
            folder_name=row["folder_name"],
            host_path=row["host_path"],
            created_at=_parse_timestamp(row["created_at"]),
            deleted_at=_parse_timestamp(row["deleted_at"]) if row["deleted_at"] else None,
        )

    def list_workspaces(self, *, deleted: bool = False) -> list[Workspace]:
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT w.*, COUNT(s.id) AS attached_sessions,
                       CASE WHEN MAX(s.updated_at) IS NULL THEN 'Not opened yet' ELSE 'Recently used' END AS last_opened
                FROM workspaces w LEFT JOIN sessions s ON s.workspace_id = w.id AND s.state != 'completed'
                WHERE w.deleted_at IS {'NOT ' if deleted else ''}NULL
                GROUP BY w.id ORDER BY w.updated_at DESC
                """
            ).fetchall()
        return [self._workspace_from_row(row) for row in rows]

    def get_workspace(self, workspace_id: UUID, *, include_deleted: bool = False) -> Workspace | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT w.*, COUNT(s.id) AS attached_sessions,
                       CASE WHEN MAX(s.updated_at) IS NULL THEN 'Not opened yet' ELSE 'Recently used' END AS last_opened
                FROM workspaces w LEFT JOIN sessions s ON s.workspace_id = w.id AND s.state != 'completed'
                WHERE w.id = ? AND (? OR w.deleted_at IS NULL) GROUP BY w.id
                """,
                (str(workspace_id), include_deleted),
            ).fetchone()
        return self._workspace_from_row(row) if row else None

    def create_workspace(self, payload: WorkspaceCreate, workspace_id: UUID, volume_name: str, folder_name: str, status: str, host_path: str | None = None) -> Workspace:
        workspace = Workspace(id=workspace_id, name=payload.name.strip(), description=payload.description.strip(), volume_name=volume_name, folder_name=folder_name, host_path=host_path, status=status, attached_sessions=0, last_opened="Not opened yet")
        now = _timestamp(workspace.created_at)
        try:
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO workspaces(id, name, description, volume_name, folder_name, host_path, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(workspace.id), workspace.name, workspace.description, workspace.volume_name, workspace.folder_name, workspace.host_path, workspace.status, now, now),
                )
                self._emit(connection, "workspace.created", {"workspace_id": str(workspace.id), "name": workspace.name})
        except sqlite3.IntegrityError as error:
            raise ValueError("A workspace with that name already exists") from error
        return workspace

    def soft_delete_workspace(self, workspace_id: UUID) -> Workspace | None:
        """Hide a workspace while preserving its data and completed session history."""
        now = _timestamp(_now())
        with self._connection() as connection:
            active = connection.execute(
                "SELECT 1 FROM sessions WHERE workspace_id = ? AND state != ? LIMIT 1",
                (str(workspace_id), SessionState.COMPLETED.value),
            ).fetchone()
            if active is not None:
                raise ValueError("stop all active sessions in this workspace before moving it to trash")
            result = connection.execute(
                "UPDATE workspaces SET status = 'deleted', deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, now, str(workspace_id)),
            )
            if result.rowcount != 1:
                return None
            self._emit(connection, "workspace.soft_deleted", {"workspace_id": str(workspace_id)})
        return self.get_workspace(workspace_id, include_deleted=True)

    def restore_workspace(self, workspace_id: UUID, status: str) -> Workspace | None:
        now = _timestamp(_now())
        with self._connection() as connection:
            result = connection.execute(
                "UPDATE workspaces SET status = ?, deleted_at = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NOT NULL",
                (status, now, str(workspace_id)),
            )
            if result.rowcount != 1:
                return None
            self._emit(connection, "workspace.restored", {"workspace_id": str(workspace_id)})
        return self.get_workspace(workspace_id)

    def workspace_has_sessions(self, workspace_id: UUID) -> bool:
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM sessions WHERE workspace_id = ? LIMIT 1", (str(workspace_id),)).fetchone() is not None

    def hard_delete_workspace(self, workspace_id: UUID) -> bool:
        """Remove metadata only after the runtime has removed durable assets."""
        with self._connection() as connection:
            has_sessions = connection.execute("SELECT 1 FROM sessions WHERE workspace_id = ? LIMIT 1", (str(workspace_id),)).fetchone()
            if has_sessions is not None:
                raise ValueError("delete all agent sessions associated with this workspace before permanently deleting it")
            workspace = connection.execute("SELECT name FROM workspaces WHERE id = ?", (str(workspace_id),)).fetchone()
            if workspace is None:
                return False
            self._emit(connection, "workspace.hard_deleted", {"workspace_id": str(workspace_id), "name": workspace["name"]})
            return connection.execute("DELETE FROM workspaces WHERE id = ?", (str(workspace_id),)).rowcount == 1

    def workspace_host_root(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = 'workspace_host_root'").fetchone()
        return row["value"] if row else None

    def set_workspace_host_root(self, host_root: str | None) -> None:
        with self._connection() as connection:
            if host_root is None:
                # An empty value records an explicit choice of Docker-managed
                # storage. A missing row means use the configured default root.
                connection.execute("INSERT INTO settings(key, value) VALUES ('workspace_host_root', '') ON CONFLICT(key) DO UPDATE SET value = excluded.value")
                stored = "docker-managed"
            else:
                connection.execute("INSERT INTO settings(key, value) VALUES ('workspace_host_root', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (host_root,))
                stored = host_root
            self._emit(connection, "workspace.storage_changed", {"workspace_host_root": stored})

    def display_timezone(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = 'display_timezone'").fetchone()
        return row["value"] if row and row["value"] else None

    def set_display_timezone(self, timezone_name: str | None) -> None:
        with self._connection() as connection:
            value = timezone_name or ""
            connection.execute(
                "INSERT INTO settings(key, value) VALUES ('display_timezone', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (value,),
            )
            self._emit(connection, "display.timezone_changed", {"timezone": timezone_name or "browser-local"})

    def add_session(self, session: Session) -> Session:
        now = _timestamp(_now())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(id, name, workspace_id, workspace_name, workspace_folder_name, harness, model, model_id, state, task, tokens, cost_usd, cost_estimate_available, input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, model_calls, tool_calls, container_id, volume_name, started_at, ended_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(session.id), session.name, str(session.workspace_id), session.workspace, session.workspace_folder_name, session.harness, session.model, str(session.model_id) if session.model_id else None, session.state, session.task, session.tokens, session.cost_usd, session.cost_estimate_available, session.input_tokens, session.output_tokens, session.cached_input_tokens, session.reasoning_tokens, session.model_calls, session.tool_calls, session.container_id, session.volume_name, _timestamp(session.started_at), _timestamp(session.ended_at) if session.ended_at else None, now),
            )
            self._emit(connection, "session.created", {"session_id": str(session.id), "name": session.name, "workspace_id": str(session.workspace_id)})
        return session

    def _session_from_row(self, row: sqlite3.Row) -> Session:
        return Session(
            id=UUID(row["id"]), name=row["name"], workspace_id=UUID(row["workspace_id"]), workspace=row["workspace_name"], workspace_folder_name=row["workspace_folder_name"] or row["workspace_id"],
            harness=row["harness"], model=row["model"], model_id=UUID(row["model_id"]) if row["model_id"] else None, state=SessionState(row["state"]), task=row["task"], tokens=row["tokens"],
            cost_usd=row["cost_usd"], cost_estimate_available=bool(row["cost_estimate_available"]), input_tokens=row["input_tokens"], output_tokens=row["output_tokens"], cached_input_tokens=row["cached_input_tokens"], reasoning_tokens=row["reasoning_tokens"], model_calls=row["model_calls"], tool_calls=row["tool_calls"], container_id=row["container_id"], volume_name=row["volume_name"],
            harness_started=bool(row["harness_started"]), started_at=_parse_timestamp(row["started_at"]), ended_at=_parse_timestamp(row["ended_at"]) if row["ended_at"] else None,
        )

    def mark_harness_started(self, session_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE sessions SET harness_started = 1, updated_at = ? WHERE id = ?", (_timestamp(_now()), str(session_id)))

    def list_sessions(self) -> list[Session]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
        return [self._session_from_row(row) for row in rows]

    def get_session(self, session_id: UUID) -> Session | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id = ?", (str(session_id),)).fetchone()
        return self._session_from_row(row) if row else None

    def update_usage(self, session_id: UUID, usage) -> None:
        """Persist normalized totals and configured or harness-reported cost."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.tokens, s.model_calls, s.cost_usd, s.cost_estimate_available,
                       m.input_cost_per_million, m.output_cost_per_million, m.cached_input_cost_per_million
                FROM sessions s LEFT JOIN registered_models m ON m.id = s.model_id
                WHERE s.id = ?
                """,
                (str(session_id),),
            ).fetchone()
            if row is None or usage.total_tokens < row["tokens"]:
                return
            calls = row["model_calls"] + usage.observed_model_calls
            normal_input_tokens = max(0, usage.input_tokens - usage.cached_input_tokens)
            rate_requirements = (
                (normal_input_tokens, row["input_cost_per_million"]),
                (usage.output_tokens, row["output_cost_per_million"]),
                (usage.cached_input_tokens, row["cached_input_cost_per_million"]),
            )
            configured_cost = None
            if all(tokens == 0 or rate is not None for tokens, rate in rate_requirements):
                configured_cost = round(sum(tokens * float(rate or 0) / 1_000_000 for tokens, rate in rate_requirements), 8)
            cost = configured_cost if configured_cost is not None else (usage.estimated_cost_usd if usage.estimated_cost_usd is not None else row["cost_usd"])
            cost_available = 1 if configured_cost is not None or usage.estimated_cost_usd is not None else row["cost_estimate_available"]
            now = _timestamp(_now())
            token_delta = usage.total_tokens - row["tokens"]
            connection.execute(
                "UPDATE sessions SET tokens = ?, input_tokens = ?, output_tokens = ?, cached_input_tokens = ?, reasoning_tokens = ?, model_calls = ?, cost_usd = ?, cost_estimate_available = ?, updated_at = ? WHERE id = ?",
                (usage.total_tokens, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens, usage.reasoning_tokens, calls, cost, cost_available, now, str(session_id)),
            )
            if token_delta:
                connection.execute("INSERT INTO token_usage_samples(session_id, tokens, occurred_at) VALUES (?, ?, ?)", (str(session_id), token_delta, now))

    def act_on_session(self, session_id: UUID, payload: SessionAction) -> Session | None:
        state_by_action = {"suspend": SessionState.SUSPENDED, "resume": SessionState.RUNNING, "stop": SessionState.COMPLETED}
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id = ?", (str(session_id),)).fetchone()
            if row is None:
                return None
            state = state_by_action[payload.action]
            now = _timestamp(_now())
            connection.execute("UPDATE sessions SET state = ?, ended_at = ?, updated_at = ? WHERE id = ?", (state, now if state == SessionState.COMPLETED else None, now, str(session_id)))
            self._emit(connection, "session.state_changed", {"session_id": str(session_id), "state": state})
        return self.get_session(session_id)

    def delete_session(self, session_id: UUID) -> bool:
        """Remove session metadata and retained terminal output, not its workspace."""
        with self._connection() as connection:
            row = connection.execute("SELECT name FROM sessions WHERE id = ?", (str(session_id),)).fetchone()
            if row is None:
                return False
            # Conversation history is a child record added after the original
            # session delete workflow. Remove it first to satisfy the foreign
            # key and avoid leaving a stale session after its container is gone.
            connection.execute("DELETE FROM session_conversation WHERE session_id = ?", (str(session_id),))
            connection.execute("DELETE FROM session_logs WHERE session_id = ?", (str(session_id),))
            connection.execute("DELETE FROM token_usage_samples WHERE session_id = ?", (str(session_id),))
            connection.execute("DELETE FROM sessions WHERE id = ?", (str(session_id),))
            self._emit(connection, "session.deleted", {"session_id": str(session_id), "name": row["name"]})
        return True

    def summary(self) -> Summary:
        sessions = self.list_sessions()
        now = _now()
        with self._connection() as connection:
            def tokens_since(hours: int) -> int:
                cutoff = _timestamp(now - timedelta(hours=hours))
                return int(connection.execute("SELECT COALESCE(SUM(tokens), 0) FROM token_usage_samples WHERE occurred_at >= ?", (cutoff,)).fetchone()[0])
        return Summary(
            total_sessions=len(sessions), running=sum(session.state == SessionState.RUNNING for session in sessions),
            suspended=sum(session.state == SessionState.SUSPENDED for session in sessions), idle=sum(session.state == SessionState.IDLE for session in sessions),
            tokens_last_hour=tokens_since(1), tokens_last_24_hours=tokens_since(24), tokens_last_7_days=tokens_since(24 * 7), estimated_cost_usd=round(sum(session.cost_usd for session in sessions if session.cost_estimate_available), 2),
            estimated_cost_available=any(session.cost_estimate_available for session in sessions),
            uptime_label="Persistent local runtime",
        )

    def telemetry(self) -> Telemetry:
        summary = self.summary()
        return Telemetry(cpu_percent=0, memory_used_gb=0, memory_total_gb=0, active_sessions=summary.running, session_tokens=sum(session.tokens for session in self.list_sessions()), latency_ms=0)

    def events(self) -> list[AppEvent]:
        import json

        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80").fetchall()
        return [AppEvent(id=row["id"], type=row["type"], occurred_at=_parse_timestamp(row["occurred_at"]), data=json.loads(row["data_json"])) for row in reversed(rows)]

    def append_log(self, session_id: UUID, stream: str, text: str) -> None:
        if not text:
            return
        # Persist a bounded, UTF-8-safe replay buffer rather than unbounded PTY output.
        text = text[-8192:]
        occurred_at = _timestamp(_now())
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO session_logs(session_id, stream, text, occurred_at) VALUES (?, ?, ?, ?)",
                (str(session_id), stream, text, occurred_at),
            )
            connection.execute(
                "DELETE FROM session_logs WHERE id NOT IN (SELECT id FROM session_logs WHERE session_id = ? ORDER BY id DESC LIMIT 800) AND session_id = ?",
                (str(session_id), str(session_id)),
            )
        # JSON Lines retains complete durable output outside SQLite's bounded
        # reconnect buffer. The directory may be a host bind mount.
        import json

        log_path = self.session_log_dir / f"{session_id}.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                os.chmod(log_path, 0o600)
                log_file.write(json.dumps({"occurred_at": occurred_at, "stream": stream, "text": text}, separators=(",", ":")) + "\n")
        except OSError:
            # Retained SQLite logs still provide operator visibility if a host
            # log mount is temporarily unavailable.
            pass

    def list_logs(self, session_id: UUID) -> list[SessionLog]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM session_logs WHERE session_id = ? ORDER BY id ASC", (str(session_id),)
            ).fetchall()
        return [
            SessionLog(id=row["id"], session_id=UUID(row["session_id"]), stream=row["stream"], text=row["text"], occurred_at=_parse_timestamp(row["occurred_at"]))
            for row in rows
        ]

    def session_log_tail(self, session_id: UUID, max_bytes: int = 262144) -> SessionLogTail:
        log_path = self.session_log_dir / f"{session_id}.jsonl"
        if not log_path.is_file():
            legacy = "".join(f"[{entry.occurred_at.isoformat()}] {entry.stream}: {entry.text}" for entry in self.list_logs(session_id))
            encoded = legacy.encode("utf-8", errors="replace")
            return SessionLogTail(text=encoded[-max_bytes:].decode("utf-8", errors="replace"), available=bool(legacy), truncated=len(encoded) > max_bytes)
        try:
            size = log_path.stat().st_size
            with log_path.open("rb") as log_file:
                if size > max_bytes:
                    log_file.seek(-max_bytes, os.SEEK_END)
                    log_file.readline()  # discard the partial JSON record
                records = log_file.read().decode("utf-8", errors="replace").splitlines()
            import json

            rendered: list[str] = []
            for record in records:
                try:
                    item = json.loads(record)
                    rendered.append(f"[{item['occurred_at']}] {item['stream']}: {item['text']}")
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue
            return SessionLogTail(text="".join(rendered), available=True, truncated=size > max_bytes)
        except OSError:
            return SessionLogTail()

    def session_log_file(self, session_id: UUID) -> Path | None:
        log_path = self.session_log_dir / f"{session_id}.jsonl"
        return log_path if log_path.is_file() else None

    def save_conversation(self, session_id: UUID, entries) -> None:
        import hashlib

        with self._connection() as connection:
            for entry in entries:
                fingerprint = hashlib.sha256(f"{entry.role}\0{entry.occurred_at}\0{entry.text}".encode()).hexdigest()
                connection.execute("INSERT OR IGNORE INTO session_conversation(session_id, fingerprint, role, text, occurred_at) VALUES (?, ?, ?, ?, ?)", (str(session_id), fingerprint, entry.role, entry.text, entry.occurred_at))

    def conversation(self, session_id: UUID) -> list[ConversationMessage]:
        with self._connection() as connection:
            rows = connection.execute("SELECT role, text, occurred_at FROM session_conversation WHERE session_id = ? ORDER BY id", (str(session_id),)).fetchall()
        messages = [ConversationMessage(role=row["role"], text=row["text"], occurred_at=_parse_timestamp(row["occurred_at"])) for row in rows]
        return sorted(messages, key=lambda message: message.occurred_at)

    def _model_from_row(self, row: sqlite3.Row) -> RegisteredModel:
        return RegisteredModel(
            id=UUID(row["id"]), provider=row["provider"], display_name=row["display_name"], model_name=row["model_name"], endpoint=row["endpoint"],
            reasoning_effort=row["reasoning_effort"], input_cost_per_million=row["input_cost_per_million"], output_cost_per_million=row["output_cost_per_million"], cached_input_cost_per_million=row["cached_input_cost_per_million"], credential_label=row["credential_label"], api_key_last_four=row["last_four"], enabled=bool(row["enabled"]), created_at=_parse_timestamp(row["created_at"]),
        )

    def list_models(self) -> list[RegisteredModel]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT m.*, c.label AS credential_label, c.last_four FROM registered_models m JOIN credentials c ON c.id = m.credential_id ORDER BY m.created_at DESC"
            ).fetchall()
        return [self._model_from_row(row) for row in rows]

    def get_model(self, model_id: UUID) -> RegisteredModel | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT m.*, c.label AS credential_label, c.last_four FROM registered_models m JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?",
                (str(model_id),),
            ).fetchone()
        return self._model_from_row(row) if row else None

    def register_model(self, payload: ModelRegistration) -> RegisteredModel:
        from uuid import uuid4

        model_id, credential_id = uuid4(), uuid4()
        now = _timestamp(_now())
        provider, model_name, endpoint = payload.provider.value, payload.model_name.strip(), payload.endpoint.strip().rstrip("/")
        if not endpoint.startswith(("https://", "http://")):
            raise ValueError("endpoint must start with http:// or https://")
        api_key = payload.api_key.strip() if payload.api_key else ""
        label = f"{provider} key · {payload.display_name.strip()}" if api_key else "No API key configured"
        try:
            # Keep a credential row for schema compatibility even when the
            # provider relies on the container's own authentication.
            encrypted = vault.encrypt(api_key)
        except VaultError as error:
            raise ValueError(str(error)) from error
        last_four = api_key[-4:]
        with self._connection() as connection:
            connection.execute("INSERT INTO credentials(id, provider, label, secret_encrypted, last_four, created_at) VALUES (?, ?, ?, ?, ?, ?)", (str(credential_id), provider, label, encrypted, last_four, now))
            connection.execute("INSERT INTO registered_models(id, provider, display_name, model_name, endpoint, reasoning_effort, input_cost_per_million, output_cost_per_million, cached_input_cost_per_million, credential_id, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)", (str(model_id), provider, payload.display_name.strip(), model_name, endpoint, payload.reasoning_effort.value, payload.input_cost_per_million, payload.output_cost_per_million, payload.cached_input_cost_per_million, str(credential_id), now))
            self._emit(connection, "model.registered", {"model_id": str(model_id), "provider": provider, "model_name": model_name, "reasoning_effort": payload.reasoning_effort.value})
        return RegisteredModel(id=model_id, provider=provider, display_name=payload.display_name.strip(), model_name=model_name, endpoint=endpoint, reasoning_effort=payload.reasoning_effort, input_cost_per_million=payload.input_cost_per_million, output_cost_per_million=payload.output_cost_per_million, cached_input_cost_per_million=payload.cached_input_cost_per_million, credential_label=label, api_key_last_four=last_four, enabled=True, created_at=_parse_timestamp(now))

    def delete_model(self, model_id: UUID) -> bool:
        """Delete a registry entry and its private credential when unused."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT credential_id, display_name FROM registered_models WHERE id = ?", (str(model_id),)
            ).fetchone()
            if row is None:
                return False
            active_count = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE model_id = ? AND state != ?",
                (str(model_id), SessionState.COMPLETED.value),
            ).fetchone()[0]
            if active_count:
                raise ValueError("Stop or complete sessions using this model before deleting it")
            connection.execute("DELETE FROM registered_models WHERE id = ?", (str(model_id),))
            connection.execute("DELETE FROM credentials WHERE id = ?", (row["credential_id"],))
            self._emit(connection, "model.deleted", {"model_id": str(model_id), "name": row["display_name"]})
        return True

    def model_secret(self, model_id: UUID) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT c.secret_encrypted FROM registered_models m JOIN credentials c ON c.id = m.credential_id WHERE m.id = ? AND m.enabled = 1", (str(model_id),)).fetchone()
        if row is None:
            return None
        return vault.decrypt(row["secret_encrypted"])


store = AgentCCStore()
