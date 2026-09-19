"""Local worktree lifecycle, with cross-process locks and durable operations."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

from .git_helper import valid_branch
from .harnesses import harnesses
from .models import Operation, RepositoryInfo, Session, SessionCreate, Worktree, default_model_label
from .runtime import RuntimeUnavailable
from .worktree_runtime import WorktreeRuntime


class WorktreeService:
    def __init__(self, store, runtime, git_runtime=None):
        self.store, self.runtime = store, runtime
        self.git = git_runtime or WorktreeRuntime(runtime)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="worktree")
        self.lock_dir = Path(store.database_path).parent / "workspace-locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, workspace_id):
        with (self.lock_dir / f"{UUID(str(workspace_id))}.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def workspace(self, workspace_id, *, deleted=False):
        workspace = self.store.get_workspace(workspace_id, include_deleted=deleted)
        if workspace is None:
            raise ValueError("Workspace not found or in trash.")
        return workspace

    def repository(self, workspace_id):
        if not self.runtime.enabled:
            return RepositoryInfo(reason="Enable the Docker runtime to inspect this repository.")
        with self.lock(workspace_id):
            return RepositoryInfo(**self.git.run("probe", self.workspace(workspace_id)))

    def model(self, payload):
        model = self.store.get_model(payload.model_id) if payload.model_id else None
        if payload.model_id and (model is None or not model.enabled):
            raise ValueError("Registered model is unavailable.")
        harnesses.for_name(payload.harness).validate_model(model)
        return model

    def provision(self, payload, workspace, *, task=None, session_id=None):
        model = self.model(payload)
        session = Session(**payload.model_dump(exclude={"checkout", "model"}),
                          id=session_id or uuid4(), workspace=workspace.name, workspace_folder_name=workspace.folder_name,
                          model=model.model_name if model else default_model_label(payload.harness),
                          worktree_id=task.id if task else None)
        container = self.runtime.provision(session, workspace, model)
        session.container_id, session.volume_name = container.container_id, container.volume_name
        return self.store.add_session(session)

    def launch(self, payload: SessionCreate, key: str | None, *, prepare_only=False):
        if not self.runtime.enabled:
            raise RuntimeUnavailable("Enable the Docker runtime before launching sessions.")
        with self.lock(payload.workspace_id):
            workspace = self.workspace(payload.workspace_id)
            self.model(payload)
            checkout = payload.checkout
            if checkout.mode == "shared":
                return self.provision(payload, workspace)
            if checkout.mode == "existing_worktree":
                task = self.store.get_worktree(checkout.worktree_id)
                if task is None or task.workspace_id != workspace.id:
                    raise ValueError("Branch task does not belong to this workspace.")
                info = self.git.run("inspect", workspace, task)
                task.tip, task.dirty = info["tip"], info["dirty"]
                self.store.save_worktree(task)
                session_id = uuid4()
                self.store.reserve_worktree(task.id, session_id)
                task = self.store.get_worktree(task.id)
                task.operation_id, task.error = None, None
                self.store.save_worktree(task)
                # Retain a failed reservation until explicitly cancelled. It may
                # correspond to a container whose create response was lost.
                try:
                    return self.provision(payload, workspace, task=task, session_id=session_id)
                except Exception:
                    task = self.store.get_worktree(task.id)
                    task.error = "Launch could not be confirmed. Cancel the pending launch before trying again."
                    self.store.save_worktree(task)
                    raise
            if not key or len(key) > 100:
                raise ValueError("An Idempotency-Key header is required for a separate branch launch.")
            task_id, session_id = uuid4(), None if prepare_only else uuid4()
            slug = re.sub(r"[^a-z0-9]+", "-", payload.name.lower()).strip("-")[:48] or "task"
            branch = valid_branch(checkout.branch or f"agentcc/{slug}-{str(task_id)[:8]}", check_with_git=False)
            task = Worktree(id=task_id, workspace_id=workspace.id, name=payload.name, branch=branch,
                            source_branch=checkout.source_branch, base_commit=checkout.expected_base_commit,
                            reserved_session_id=session_id,
                            host_path=os.path.join(os.path.dirname(workspace.host_path), f"agentcc-task-{task_id}") if workspace.host_path else None)
            operation = Operation(workspace_id=workspace.id, worktree_id=task.id, session_id=session_id)
            task.operation_id = operation.id
            try:
                operation, created = self.store.begin_worktree_operation(task, operation, payload, key)
            except sqlite3.IntegrityError as error:
                raise ValueError("That task branch already exists. Choose another name or continue the existing task.") from error
            if created:
                self.executor.submit(self.run_operation, operation.id)
            return operation

    def run_operation(self, operation_id):
        operation = self.store.get_operation(operation_id)
        if operation is None:
            return
        with self.lock(operation.workspace_id):
            operation = self.store.get_operation(operation_id)
            if operation.state not in {"pending", "running"}:
                return
            task = self.store.get_worktree(operation.worktree_id)
            try:
                existing = self.store.get_session(operation.session_id) if operation.session_id else None
                if existing is None:
                    workspace = self.workspace(operation.workspace_id)
                    payload = self.store.operation_payload(operation_id)
                    operation.state, operation.error = "running", None
                    self.store.save_operation(operation)
                    info = self.git.run("create", workspace, task, use_committed_version=payload.checkout.use_committed_version,
                                        reuse_git_identity=payload.checkout.reuse_git_identity)
                    task.tip, task.dirty, task.state, task.error = info["tip"], info["dirty"], "ready", None
                    self.store.save_worktree(task)
                    if operation.session_id:
                        self.provision(payload, workspace, task=task, session_id=operation.session_id)
                operation.state, operation.error = "completed", None
            except Exception as error:
                operation.state = "failed"
                operation.error = str(error) if isinstance(error, (ValueError, RuntimeUnavailable)) else "Task launch failed. Its files and reservation were retained for recovery."
                task.error = operation.error
                if task.state == "creating":
                    task.state = "needs_attention"
                self.store.save_worktree(task)
            self.store.save_operation(operation)

    def retry(self, operation_id):
        operation = self.store.get_operation(operation_id)
        if operation is None:
            raise ValueError("Operation not found.")
        with self.lock(operation.workspace_id):
            operation = self.store.get_operation(operation_id)
            if operation.state == "failed":
                self.workspace(operation.workspace_id)
                task = self.store.get_worktree(operation.worktree_id)
                if task.state not in {"ready", "needs_attention", "creating"}:
                    raise ValueError("This task can no longer retry its original launch.")
                if operation.session_id and task.reserved_session_id != operation.session_id:
                    raise ValueError("This launch no longer owns the task reservation.")
                operation.state, operation.error = "pending", None
                self.store.save_operation(operation)
                self.executor.submit(self.run_operation, operation.id)
            return operation

    def cancel_pending(self, task_id):
        task = self.store.get_worktree(task_id)
        if task is None:
            raise ValueError("Task not found.")
        with self.lock(task.workspace_id):
            task = self.store.get_worktree(task_id)
            if self.store.worktree_has_sessions(task.id, active_only=True):
                raise ValueError("Stop the active session first.")
            if task.reserved_session_id:
                workspace = self.workspace(task.workspace_id, deleted=True)
                session = Session(id=task.reserved_session_id, name=task.name, workspace_id=workspace.id,
                                  workspace=workspace.name, workspace_folder_name=workspace.folder_name, task="", worktree_id=task.id)
                self.runtime.cancel_pending_session(session)
                self.store.release_worktree(task.id, session.id)
            if task.operation_id:
                operation = self.store.get_operation(task.operation_id)
                if operation.state != "completed":
                    operation.state = "cancelled"
                    self.store.save_operation(operation)
            task = self.store.get_worktree(task.id)
            task.error = None
            self.store.save_worktree(task)
            return task

    def task_action(self, task_id, action, *, expected_tip=None):
        task = self.store.get_worktree(task_id)
        if task is None:
            raise ValueError("Branch task not found.")
        with self.lock(task.workspace_id):
            task = self.store.get_worktree(task_id)
            workspace = self.workspace(task.workspace_id, deleted=action != "restore")
            if action == "inspect":
                if task.state == "removed" and not task.tip:
                    return task
                info = self.git.run("branch" if task.state == "removed" else "inspect", workspace, task)
                task.tip, task.dirty = info["tip"], info["dirty"]
            else:
                if task.reserved_session_id or self.store.worktree_has_sessions(task.id, active_only=True):
                    raise ValueError("Stop the active or pending task session first.")
                if action == "archive":
                    if task.state != "ready":
                        raise ValueError("Only ready tasks can be archived.")
                    task.state = "archived"
                elif action == "restore":
                    if task.state != "archived":
                        raise ValueError("This task is not archived.")
                    self.git.run("inspect", workspace, task)
                    task.state = "ready"
                elif action == "export":
                    info = self.git.run("export", workspace, task, expected_tip=expected_tip)
                    task.tip = task.exported_commit = info["tip"]
                elif action == "remove":
                    if self.store.worktree_has_sessions(task.id):
                        raise ValueError("Delete retained sessions for this task before removing its checkout.")
                    if task.state == "removed":
                        return task
                    if task.state != "removing":
                        info = self.git.run("remove", workspace, task)
                        task.tip, task.dirty = info["tip"], info["dirty"]
                    task.state = "removing"
                    self.store.save_worktree(task)
                    self.git.remove_checkout_storage(task)
                    task.state = "removed"
                else:
                    raise ValueError("Unknown task action.")
            task.error = None
            self.store.save_worktree(task)
            return task

    def recover(self):
        if self.runtime.enabled:
            # A crash after persisting stop but before releasing its reservation
            # must not permanently prevent a subsequent session.
            for session in self.store.list_sessions():
                if session.worktree_id and session.state == "completed":
                    with self.lock(session.workspace_id):
                        self.store.release_worktree(session.worktree_id, session.id)
            for operation in self.store.unfinished_operations():
                self.executor.submit(self.run_operation, operation.id)
