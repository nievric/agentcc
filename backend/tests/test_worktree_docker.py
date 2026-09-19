"""Opt-in Docker integration tests; no provider credentials or model calls.

Build the two fixture images described in backend/tests/README.md first.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from support import make_store, make_workspace
from app.checkout import checkout_path, repository_volume, worktree_volume
from app.harnesses import HarnessLaunch
from app.models import CheckoutRequest, SessionAction, SessionCreate
from app.runtime import DockerContainerRuntime
from app.worktrees import WorktreeService


@unittest.skipUnless(os.getenv("AGENTCC_DOCKER_TESTS") == "1", "opt-in Docker integration suite")
class DockerWorktreeTests(unittest.TestCase):
    host_backed = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentcc-docker-test-")
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"AGENTCC_RUNTIME_ENABLED": "true",
            "AGENTCC_SESSION_IMAGE": "agentcc-worktree-test:dev", "AGENTCC_CODEX_SESSION_IMAGE": "agentcc-worktree-test:dev",
            "AGENTCC_GIT_HELPER_IMAGE": "agentcc-git-helper:dev", "AGENTCC_WORKSPACE_HOST_ROOTS": "",
            "AGENTCC_SESSION_NETWORK": f"agentcc-test-{uuid4()}"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = make_store(Path(self.temp.name) / "data")
        self.workspace = make_workspace(self.store)
        self.runtime = DockerContainerRuntime()
        if self.host_backed:
            # These paths belong to the Docker daemon host, including on Desktop.
            self.runtime.workspace_host_roots = ("/tmp",)
            self.workspace.host_path = f"/tmp/{self.workspace.folder_name}"
            with self.store._connection() as connection:
                connection.execute("UPDATE workspaces SET host_path = ? WHERE id = ?", (self.workspace.host_path, str(self.workspace.id)))
        self.client = self.runtime._client()
        self.service = WorktreeService(self.store, self.runtime)
        self.addCleanup(self.cleanup_runtime)
        self.runtime.create_workspace_volume(self.workspace)
        # Match a normal coder-owned original checkout, writable by workspace group.
        program = """import os,subprocess
os.umask(0o002)
os.chdir('/source')
def git(*args): subprocess.run(['git','-c','safe.directory=/source', *args],check=True,stdout=subprocess.DEVNULL)
git('init','-b','main'); git('config','user.name','Test'); git('config','user.email','test@example.invalid')
open('file.txt','w').write('original\\n')
git('add','.'); git('commit','-m','Initial')
for root,dirs,files in os.walk('/source'):
 os.chown(root,1000,10001); os.chmod(root,0o2775)
 for name in files: os.chown(os.path.join(root,name),1000,10001)
"""
        self.service.git._run(self.client, ["-c", program], {self.workspace.volume_name: {"bind": "/source", "mode": "rw"}}, root=True)
        self.base = self.service.repository(self.workspace.id).head

    def cleanup_runtime(self):
        self.service.executor.shutdown(wait=True)
        for session in self.store.list_sessions():
            self.runtime.delete_session(session)
        for task in self.store.list_worktrees(self.workspace.id):
            try:
                self.client.volumes.get(worktree_volume(task.id)).remove()
            except Exception:
                pass
            if task.host_path:
                self.service.git._run(self.client, ["-c", "import shutil,sys; shutil.rmtree('/host/'+sys.argv[1],ignore_errors=True)", f"agentcc-task-{task.id}"],
                    {"/tmp": {"bind": "/host", "mode": "rw"}}, root=True)
        for name in [repository_volume(self.workspace.id), self.workspace.volume_name]:
            try:
                self.client.volumes.get(name).remove()
            except Exception:
                pass
        if self.workspace.host_path:
            self.runtime.delete_workspace_storage(self.workspace)
        try:
            self.client.networks.get(self.runtime.network_name).remove()
        except Exception:
            pass
        self.client.close()

    def launch(self, name, checkout=None):
        payload = SessionCreate(name=name, workspace_id=self.workspace.id, checkout=checkout or
            CheckoutRequest(mode="new_worktree", source_branch="main", expected_base_commit=self.base))
        result = self.service.launch(payload, str(uuid4()))
        if hasattr(result, "worktree_id") and hasattr(result, "session_id"):
            deadline = time.monotonic() + 90
            while result.state in {"pending", "running"} and time.monotonic() < deadline:
                time.sleep(.1)
                result = self.store.get_operation(result.id)
            self.assertEqual(result.state, "completed", result.error)
            result = self.store.get_session(result.session_id)
        container = self.client.containers.get(result.container_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code, _ = container.exec_run(["test", "-f", "/home/coder/.gitconfig"])
            if code == 0:
                break
            time.sleep(.1)
        container.reload()
        self.assertEqual(container.status, "running", container.logs().decode())
        return result, container

    def command(self, container, command, *, user="agent", cwd=None):
        result = container.exec_run(command, user=user, workdir=cwd)
        self.assertEqual(result.exit_code, 0, result.output.decode())
        return result.output.decode().strip()

    def stop(self, session):
        self.runtime.stop(session.container_id)
        self.store.act_on_session(session.id, SessionAction(action="stop"))
        self.store.release_worktree(session.worktree_id, session.id)

    def test_real_volumes_parallel_sessions_continue_export_and_remove(self):
        first, a = self.launch("First")
        second, b = self.launch("Second")
        task = self.store.get_worktree(first.worktree_id)
        path = checkout_path(first)
        mounts = {item["Name"] for item in a.attrs["Mounts"] if item["Type"] == "volume"}
        self.assertEqual(mounts, {first.volume_name, repository_volume(self.workspace.id), worktree_volume(task.id)})
        self.assertNotIn(self.workspace.volume_name, mounts)
        self.assertNotIn(worktree_volume(second.worktree_id), mounts)
        self.assertEqual(a.attrs["Config"]["WorkingDir"], path)
        self.assertIn(f"WORKSPACE_ROOT={path}", a.attrs["Config"]["Env"])
        self.assertEqual(self.runtime.provision(first, self.workspace).container_id, a.id)
        self.command(a, ["git", "status", "--porcelain"], user="coder", cwd=path)
        self.command(a, ["sh", "-c", "echo changed > file.txt; echo retained > local.txt"], cwd=path)
        self.assertEqual(self.command(b, ["cat", "file.txt"], cwd=checkout_path(second)), "original")
        self.command(a, ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "add", "file.txt"], cwd=path)
        self.command(a, ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Task result"], cwd=path)
        tip = self.command(a, ["git", "rev-parse", "HEAD"], cwd=path)
        # The same resolver drives the persistent harness terminal's cwd.
        launch = HarnessLaunch(entrypoint="sleep 300", environment={"HOME": "/home/agent"})
        self.runtime._ensure_tmux_session(self.client, a.id, launch, path)
        self.assertEqual(self.command(a, ["tmux", "display-message", "-p", "-t", "agentcc:0.0", "#{pane_current_path}"]), path)
        a.stop(timeout=1)
        self.assertTrue(self.runtime.recover_running_session(first))
        self.runtime._ensure_tmux_session(self.client, a.id, launch, path)
        self.assertEqual(self.command(a, ["tmux", "display-message", "-p", "-t", "agentcc:0.0", "#{pane_current_path}"]), path)
        self.runtime.suspend(first.container_id)
        with self.assertRaisesRegex(ValueError, "active"):
            self.service.launch(SessionCreate(name="Duplicate", workspace_id=self.workspace.id,
                checkout=CheckoutRequest(mode="existing_worktree", worktree_id=task.id)), None)
        self.runtime.resume(first.container_id)
        self.stop(first)
        self.runtime.delete_session(first)
        self.store.delete_session(first.id)
        resumed, c = self.launch("Continue", CheckoutRequest(mode="existing_worktree", worktree_id=task.id))
        self.assertEqual(self.command(c, ["cat", "local.txt"], cwd=path), "retained")
        self.command(c, ["rm", "local.txt"], cwd=path)
        self.stop(resumed)
        exported = self.service.task_action(task.id, "export", expected_tip=tip)
        self.assertEqual(exported.exported_commit, tip)
        source_info = self.service.repository(self.workspace.id)
        self.assertEqual(source_info.head, self.base)
        self.assertEqual(source_info.branches[task.branch], tip)
        self.runtime.delete_session(resumed)
        self.store.delete_session(resumed.id)
        self.assertEqual(self.service.task_action(task.id, "remove").state, "removed")
        self.assertEqual(self.service.task_action(task.id, "export", expected_tip=tip).tip, tip)
        self.assertTrue(self.client.volumes.get(worktree_volume(second.worktree_id)))


class HostBindDockerWorktreeTests(DockerWorktreeTests):
    host_backed = True
