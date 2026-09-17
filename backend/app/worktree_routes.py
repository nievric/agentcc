from uuid import UUID

from fastapi import APIRouter, Header, HTTPException

from .models import CheckoutRequest, Operation, RepositoryInfo, SessionCreate, Worktree, WorktreeCreate, WorktreeExport
from .runtime import RuntimeUnavailable


def worktree_router(service):
    router = APIRouter(prefix="/api/v1")

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RuntimeUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.get("/workspaces/{workspace_id}/repository", response_model=RepositoryInfo)
    def repository(workspace_id: UUID):
        return call(service.repository, workspace_id)

    @router.get("/workspaces/{workspace_id}/worktrees", response_model=list[Worktree])
    def tasks(workspace_id: UUID):
        call(service.workspace, workspace_id, deleted=True)
        return service.store.list_worktrees(workspace_id)

    @router.post("/workspaces/{workspace_id}/worktrees", response_model=Operation, status_code=202)
    def prepare_task(workspace_id: UUID, payload: WorktreeCreate, idempotency_key: str | None = Header(default=None)):
        request = SessionCreate(name=payload.name, workspace_id=workspace_id,
            checkout=CheckoutRequest(mode="new_worktree", **payload.model_dump(exclude={"name"})))
        return call(service.launch, request, idempotency_key, prepare_only=True)

    @router.get("/worktrees/{worktree_id}", response_model=Worktree)
    def inspect_task(worktree_id: UUID):
        return call(service.task_action, worktree_id, "inspect")

    @router.get("/operations/{operation_id}", response_model=Operation)
    def operation(operation_id: UUID):
        result = service.store.get_operation(operation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Operation not found.")
        return result

    @router.post("/operations/{operation_id}/retry", response_model=Operation)
    def retry(operation_id: UUID):
        return call(service.retry, operation_id)

    @router.post("/worktrees/{worktree_id}/cancel-launch", response_model=Worktree)
    def cancel(worktree_id: UUID):
        return call(service.cancel_pending, worktree_id)

    @router.post("/worktrees/{worktree_id}/archive", response_model=Worktree)
    def archive(worktree_id: UUID):
        return call(service.task_action, worktree_id, "archive")

    @router.post("/worktrees/{worktree_id}/restore", response_model=Worktree)
    def restore(worktree_id: UUID):
        return call(service.task_action, worktree_id, "restore")

    @router.post("/worktrees/{worktree_id}/remove", response_model=Worktree)
    def remove(worktree_id: UUID):
        return call(service.task_action, worktree_id, "remove")

    @router.post("/worktrees/{worktree_id}/export", response_model=Worktree)
    def export(worktree_id: UUID, payload: WorktreeExport):
        return call(service.task_action, worktree_id, "export", expected_tip=payload.expected_tip)

    return router
