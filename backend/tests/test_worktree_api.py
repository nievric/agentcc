from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from support import FakeRuntime, LocalGitRuntime, make_store, make_workspace, seed
from test_worktree_service import DeferredExecutor
from app import main


class WorktreeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentcc-api-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = make_store(root / "db")
        self.workspace = make_workspace(self.store)
        self.source, self.base = seed(root)
        runtime = FakeRuntime()
        patches = [patch.object(main, "store", self.store), patch.object(main, "runtime", runtime),
                   patch.object(main.worktrees, "store", self.store), patch.object(main.worktrees, "runtime", runtime),
                   patch.object(main.worktrees, "git", LocalGitRuntime(root, self.source)),
                   patch.object(main.worktrees, "executor", DeferredExecutor())]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.payload = {"name": "API task", "workspace_id": str(self.workspace.id), "checkout": {
            "mode": "new_worktree", "source_branch": "main", "expected_base_commit": self.base}}

    def test_async_contract_idempotency_and_validation(self):
        self.assertEqual(self.client.post("/api/v1/sessions", json=self.payload).status_code, 409)
        headers = {"Idempotency-Key": "create-once"}
        response = self.client.post("/api/v1/sessions", json=self.payload, headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        operation = response.json()
        self.assertEqual(self.client.post("/api/v1/sessions", json=self.payload, headers=headers).json()["id"], operation["id"])
        self.assertEqual(self.client.post("/api/v1/sessions", json={**self.payload, "name": "changed"}, headers=headers).status_code, 409)
        main.worktrees.run_operation(operation["id"])
        self.assertEqual(self.client.get(f"/api/v1/operations/{operation['id']}").json()["state"], "completed")
        self.assertEqual(self.client.get("/api/v1/system/summary").json()["running"], 1)
        self.assertEqual(self.client.get("/api/v1/telemetry").status_code, 200)
        invalid = {**self.payload, "checkout": {"mode": "existing_worktree"}}
        self.assertEqual(self.client.post("/api/v1/sessions", json=invalid).status_code, 422)

    def test_prepare_without_session_export_and_explicit_history_deletion(self):
        response = self.client.post(f"/api/v1/workspaces/{self.workspace.id}/worktrees",
            headers={"Idempotency-Key": "prepare"}, json={"name": "Prepare only", "expected_base_commit": self.base})
        self.assertEqual(response.status_code, 202, response.text)
        operation = response.json()
        main.worktrees.run_operation(operation["id"])
        self.assertEqual(self.store.list_sessions(), [])
        task_id = operation["worktree_id"]
        self.assertEqual(self.client.get(f"/api/v1/worktrees/{task_id}").json()["state"], "ready")
        self.assertEqual(self.client.post(f"/api/v1/worktrees/{task_id}/remove").status_code, 200)
        url = f"/api/v1/workspaces/{self.workspace.id}"
        self.assertEqual(self.client.request("DELETE", url, json={"mode": "hard"}).status_code, 409)
        self.assertEqual(self.client.request("DELETE", url, json={"mode": "hard", "discard_task_history": True}).status_code, 204)

    def test_stopped_sessions_cannot_resume_and_release_task(self):
        response = self.client.post("/api/v1/sessions", json=self.payload, headers={"Idempotency-Key": "stop"})
        op = response.json()
        main.worktrees.run_operation(op["id"])
        url = f"/api/v1/sessions/{op['session_id']}/actions"
        self.assertEqual(self.client.post(url, json={"action": "stop"}).status_code, 200)
        self.assertEqual(self.client.post(url, json={"action": "resume"}).status_code, 409)
        self.assertIsNone(self.client.get(f"/api/v1/worktrees/{op['worktree_id']}").json()["reserved_session_id"])
        self.assertEqual(self.client.delete(f"/api/v1/sessions/{op['session_id']}").status_code, 204)
