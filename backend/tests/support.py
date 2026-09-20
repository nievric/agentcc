import atexit
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

_boot = tempfile.TemporaryDirectory(prefix="agentcc-test-boot-")
atexit.register(_boot.cleanup)
os.environ["AGENTCC_DATA_DIR"] = _boot.name
os.environ.pop("AGENTCC_DATABASE", None)
os.environ.pop("AGENTCC_SESSION_LOG_DIR", None)
os.environ["AGENTCC_RUNTIME_ENABLED"] = "false"

from app.git_helper import execute, git, output
from app.models import AgentActivity, WorkspaceCreate
from app.runtime import DockerContainerRuntime, ProvisionedContainer
from app.store import AgentCCStore


def make_store(root):
    with patch.dict(os.environ, {"AGENTCC_DATA_DIR": str(root)}):
        return AgentCCStore()


def make_workspace(store):
    identifier = uuid4()
    return store.create_workspace(WorkspaceCreate(name=f"project-{str(identifier)[:8]}"), identifier,
                                  f"agentcc-workspace-{identifier}", str(identifier), "ready")


def seed(root):
    source = Path(root) / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test Author")
    git(source, "config", "user.email", "tests@example.invalid")
    (source / "file.txt").write_text("original\n")
    (source / ".gitignore").write_text(".env\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Initial")
    return source, output(source, "rev-parse", "HEAD")


class LocalGitRuntime:
    def __init__(self, root, source):
        self.root, self.source = Path(root), source

    def run(self, action, workspace, task=None, **options):
        payload = {"action": action, "source": str(self.source), **options}
        if task:
            payload.update(id=str(task.id), repo=str(self.root / "repo.git"),
                           checkout=str(self.root / str(task.id) / "checkout"), branch=task.branch,
                           base_commit=task.base_commit, source_branch=task.source_branch,
                           exported_commit=task.exported_commit)
        return execute(payload)

    def remove_checkout_storage(self, task):
        path = self.root / str(task.id)
        if path.exists():
            path.rmdir()

    def remove_repository_storage(self, workspace_id):
        pass


class FakeRuntime(DockerContainerRuntime):
    def __init__(self):
        super().__init__()
        self.enabled = True
        self.provisions = []

    def provision(self, session, workspace, model=None):
        self.provisions.append(session)
        return ProvisionedContainer(f"container-{session.id}", f"session-volume-{session.id}")

    def agent_activity(self, session):
        return AgentActivity.UNKNOWN

    def collect_usage(self, session):
        return None

    def collect_conversation(self, session):
        return []

    def stop(self, container_id):
        pass

    def delete_session(self, session):
        pass

    def cancel_pending_session(self, session):
        pass

    def delete_workspace_storage(self, workspace):
        pass
