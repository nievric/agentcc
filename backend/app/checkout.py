"""Stable paths shared by Docker provisioning, helpers, and harness adapters."""

from uuid import UUID

from .models import Session


def repository_volume(workspace_id: UUID) -> str:
    return f"agentcc-repo-{workspace_id}"


def repository_root(workspace_id: UUID) -> str:
    return f"/var/lib/agentcc/repos/{workspace_id}"


def worktree_volume(worktree_id: UUID) -> str:
    return f"agentcc-worktree-{worktree_id}"


def worktree_root(worktree_id: UUID) -> str:
    return f"/workspaces/tasks/{worktree_id}"


def checkout_path(session: Session) -> str:
    if session.worktree_id:
        return f"{worktree_root(session.worktree_id)}/checkout"
    return f"/workspaces/shared/{session.workspace_folder_name}"
