"""Persistence for durable checkouts and recoverable launch operations."""

import hashlib
from uuid import UUID

from .models import Operation, SessionCreate, Worktree


class WorktreeStoreMixin:
    def _initialize_worktrees(self, connection):
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS worktrees (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                branch TEXT NOT NULL,
                state TEXT NOT NULL,
                reserved_session_id TEXT,
                data_json TEXT NOT NULL,
                UNIQUE(workspace_id, branch)
            );
            CREATE TABLE IF NOT EXISTS worktree_operations (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                worktree_id TEXT NOT NULL REFERENCES worktrees(id),
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_worktree_writer ON sessions(worktree_id)
                WHERE worktree_id IS NOT NULL AND state != 'completed';
        """)

    def get_worktree(self, worktree_id: UUID) -> Worktree | None:
        with self._connection() as connection:
            row = connection.execute("SELECT data_json FROM worktrees WHERE id = ?", (str(worktree_id),)).fetchone()
        return Worktree.model_validate_json(row[0]) if row else None

    def list_worktrees(self, workspace_id: UUID) -> list[Worktree]:
        with self._connection() as connection:
            rows = connection.execute("SELECT data_json FROM worktrees WHERE workspace_id = ? ORDER BY rowid", (str(workspace_id),)).fetchall()
        return [Worktree.model_validate_json(row[0]) for row in rows]

    def save_worktree(self, worktree: Worktree):
        with self._connection() as connection:
            connection.execute("UPDATE worktrees SET state = ?, reserved_session_id = ?, data_json = ? WHERE id = ?",
                               (worktree.state, str(worktree.reserved_session_id) if worktree.reserved_session_id else None,
                                worktree.model_dump_json(), str(worktree.id)))
            self._emit(connection, "worktree.updated", {"workspace_id": str(worktree.workspace_id), "worktree_id": str(worktree.id), "state": worktree.state})

    def begin_worktree_operation(self, worktree: Worktree, operation: Operation, payload: SessionCreate, key: str) -> tuple[Operation, bool]:
        request_json = payload.model_dump_json()
        # A prepare-only request must not reuse a key from a session launch.
        request_kind = "launch:" if operation.session_id else "prepare:"
        digest = hashlib.sha256((request_kind + request_json).encode()).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT request_hash, data_json FROM worktree_operations WHERE idempotency_key = ?", (key,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError("This launch key was already used for a different request.")
                return Operation.model_validate_json(existing[1]), False
            connection.execute("INSERT INTO worktrees VALUES (?, ?, ?, ?, ?, ?)",
                               (str(worktree.id), str(worktree.workspace_id), worktree.branch, worktree.state,
                                str(worktree.reserved_session_id) if worktree.reserved_session_id else None, worktree.model_dump_json()))
            connection.execute("INSERT INTO worktree_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (str(operation.id), str(operation.workspace_id), str(operation.worktree_id), key, digest,
                                request_json, operation.state, operation.model_dump_json()))
        return operation, True

    def get_operation(self, operation_id: UUID) -> Operation | None:
        with self._connection() as connection:
            row = connection.execute("SELECT data_json FROM worktree_operations WHERE id = ?", (str(operation_id),)).fetchone()
        return Operation.model_validate_json(row[0]) if row else None

    def operation_payload(self, operation_id: UUID) -> SessionCreate:
        with self._connection() as connection:
            row = connection.execute("SELECT request_json FROM worktree_operations WHERE id = ?", (str(operation_id),)).fetchone()
        return SessionCreate.model_validate_json(row[0])

    def save_operation(self, operation: Operation):
        with self._connection() as connection:
            connection.execute("UPDATE worktree_operations SET state = ?, data_json = ? WHERE id = ?",
                               (operation.state, operation.model_dump_json(), str(operation.id)))

    def unfinished_operations(self) -> list[Operation]:
        with self._connection() as connection:
            rows = connection.execute("SELECT data_json FROM worktree_operations WHERE state IN ('pending', 'running')").fetchall()
        return [Operation.model_validate_json(row[0]) for row in rows]

    def reserve_worktree(self, worktree_id: UUID, session_id: UUID):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT data_json FROM worktrees WHERE id = ?", (str(worktree_id),)).fetchone()
            if row is None:
                raise ValueError("Branch task not found.")
            task = Worktree.model_validate_json(row[0])
            if task.state != "ready":
                raise ValueError("Restore or repair the task before launching a session.")
            active = connection.execute("SELECT id FROM sessions WHERE worktree_id = ? AND state != 'completed'", (str(worktree_id),)).fetchone()
            if active or (task.reserved_session_id and task.reserved_session_id != session_id):
                raise ValueError("This task already has an active or pending session. Open it or stop it first.")
            task.reserved_session_id = session_id
            connection.execute("UPDATE worktrees SET reserved_session_id = ?, data_json = ? WHERE id = ?",
                               (str(session_id), task.model_dump_json(), str(worktree_id)))

    def release_worktree(self, worktree_id: UUID, session_id: UUID):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT data_json FROM worktrees WHERE id = ?", (str(worktree_id),)).fetchone()
            if row:
                task = Worktree.model_validate_json(row[0])
                if task.reserved_session_id == session_id:
                    task.reserved_session_id = None
                    connection.execute("UPDATE worktrees SET reserved_session_id = NULL, data_json = ? WHERE id = ?", (task.model_dump_json(), str(worktree_id)))

    def worktree_has_sessions(self, worktree_id: UUID, *, active_only=False) -> bool:
        with self._connection() as connection:
            suffix = " AND state != 'completed'" if active_only else ""
            return connection.execute(f"SELECT 1 FROM sessions WHERE worktree_id = ?{suffix}", (str(worktree_id),)).fetchone() is not None

    def guard_worktree_assets(self, workspace_id: UUID, *, hard=False, discard_history=False):
        tasks = self.list_worktrees(workspace_id)
        if any(task.reserved_session_id or task.state in {"creating", "removing"} for task in tasks):
            raise ValueError("Stop pending and active branch tasks before deleting this workspace.")
        if hard and any(task.state != "removed" for task in tasks):
            raise ValueError("Remove retained branch task checkouts before permanently deleting this workspace.")
        if hard and tasks and not discard_history:
            raise ValueError("This workspace retains task branches. Explicitly confirm deleting task history.")
