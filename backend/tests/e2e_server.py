"""Browser fixture: production FastAPI/SQLite/Git with a fake session runtime.

No production database, Docker socket, external credentials or model calls.
"""
from pathlib import Path
from uuid import uuid4

from support import _boot, FakeRuntime, LocalGitRuntime, make_store, seed
from app.models import WorkspaceCreate
from app import main


class BrowserRuntime(FakeRuntime):
    def workspace_usage(self, workspace):
        return None

    def capture_terminal_output(self, session):
        return None

    def recover_running_session(self, session):
        return False


class BrowserGit:
    def __init__(self):
        self.repositories = {}

    def run(self, action, workspace, task=None, **options):
        return self.repositories[workspace.id].run(action, workspace, task, **options)

    def remove_checkout_storage(self, task):
        self.repositories[task.workspace_id].remove_checkout_storage(task)

    def remove_repository_storage(self, workspace_id):
        self.repositories[workspace_id].remove_repository_storage(workspace_id)


root = Path(_boot.name)
main.store = make_store(root / "browser-db")
main.runtime = BrowserRuntime()
main.worktrees.store = main.store
main.worktrees.runtime = main.runtime
main.worktrees.git = BrowserGit()
for name in ("Clean project", "Dirty project", "Plain folder"):
    identifier = uuid4()
    workspace = main.store.create_workspace(WorkspaceCreate(name=name), identifier, f"test-{identifier}", str(identifier), "ready")
    folder = root / str(identifier)
    folder.mkdir()
    if name == "Plain folder":
        source = folder / "source"
        source.mkdir()
    else:
        source, _ = seed(folder)
        if name == "Dirty project":
            (source / "local.txt").write_text("local edits remain here")
    main.worktrees.git.repositories[identifier] = LocalGitRuntime(folder, source)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=19090, log_level="warning")
