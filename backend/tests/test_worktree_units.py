from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from datetime import datetime, timezone

from docker.errors import APIError, NotFound
from pydantic import ValidationError

from support import make_store, make_workspace
from app.checkout import checkout_path, repository_root, repository_volume, worktree_root, worktree_volume
from app.git_helper import valid_branch
from app.models import CheckoutRequest, RegisteredModel, Session, Worktree
from app.harnesses import harnesses
from app.runtime import DockerContainerRuntime, RuntimeUnavailable
from app.worktree_runtime import WorktreeRuntime


class WorktreeUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentcc-unit-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = make_store(Path(self.temp.name))
        self.workspace = make_workspace(self.store)
        self.session = Session(name="Test", workspace_id=self.workspace.id, workspace=self.workspace.name,
                               workspace_folder_name=self.workspace.folder_name, task="")

    def test_validation_and_branch_check_never_need_git_in_api_image(self):
        with patch("app.git_helper.subprocess.run", side_effect=AssertionError("API must not invoke Git")):
            self.assertEqual(valid_branch("agentcc/task-123", check_with_git=False), "agentcc/task-123")
            for name in ["--force", "x/../main", "bad.lock", "one//two", "a[0]", "a\\b", "a\n"]:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    valid_branch(name, check_with_git=False)
        for payload in [dict(mode="new_worktree"), dict(mode="existing_worktree"), dict(mode="shared", worktree_id=uuid4())]:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                CheckoutRequest(**payload)

    def test_all_four_harnesses_mount_only_their_task_and_use_one_cwd(self):
        runtime, client = DockerContainerRuntime(), Mock()
        self.session.worktree_id = uuid4()
        expected = {"private": {"bind": "/workspaces/session", "mode": "rw"},
                    repository_volume(self.workspace.id): {"bind": repository_root(self.workspace.id), "mode": "rw"},
                    worktree_volume(self.session.worktree_id): {"bind": worktree_root(self.session.worktree_id), "mode": "rw"}}
        for harness in ["Codex", "Hermes", "Claude Code", "Kilo Code"]:
            with self.subTest(harness=harness):
                self.session.harness = harness
                runtime._run_session_container(client, self.session, self.workspace, None, "private", "container")
                options = client.containers.run.call_args.kwargs
                self.assertEqual(options["volumes"], expected)
                self.assertEqual(options["working_dir"], checkout_path(self.session))
                self.assertEqual(options["environment"]["WORKSPACE_ROOT"], checkout_path(self.session))
                self.assertEqual(options["ports"], {})

    def test_task_provision_does_not_create_or_mount_original_workspace_storage(self):
        runtime, client = DockerContainerRuntime(), Mock()
        self.session.worktree_id = uuid4()
        client.containers.get.side_effect = NotFound("missing pending container")
        client.volumes.create.return_value.attrs = {"Labels": {"agentcc.managed": "true", "agentcc.kind": "session-volume",
            "agentcc.session_id": str(self.session.id), "agentcc.workspace": self.session.workspace}}
        with patch.object(runtime, "_client", return_value=client), patch.object(runtime, "_network"), \
             patch.object(runtime, "_connect_control_plane"), patch.object(runtime, "create_workspace_volume") as source:
            runtime.provision(self.session, self.workspace)
        source.assert_not_called()
        self.assertNotIn(self.workspace.volume_name, client.containers.run.call_args.kwargs["volumes"])

    def test_legacy_database_migration_preserves_original_session(self):
        self.store.add_session(self.session)
        with self.store._connection() as connection:
            connection.execute("DROP INDEX idx_worktree_writer")
            connection.execute("ALTER TABLE sessions DROP COLUMN worktree_id")
            connection.execute("DROP TABLE worktree_operations")
            connection.execute("DROP TABLE worktrees")
        reopened = make_store(Path(self.temp.name))
        existing = reopened.get_session(self.session.id)
        self.assertIsNone(existing.worktree_id)
        self.assertEqual(existing.name, self.session.name)
        self.assertEqual(checkout_path(existing), f"/workspaces/shared/{self.workspace.folder_name}")
        self.assertEqual(reopened.summary().running, 1)
        self.assertEqual(reopened.list_worktrees(self.workspace.id), [])

    def test_hermes_tool_cwd_matches_session_checkout(self):
        self.session.worktree_id = uuid4()
        model = RegisteredModel(id=uuid4(), provider="OpenRouter", display_name="Test", model_name="test/model",
            endpoint="https://openrouter.ai/api/v1", credential_label="test", api_key_last_four="", enabled=True,
            created_at=datetime.now(timezone.utc))
        launch = harnesses.for_name("Hermes").prepare(self.session, model, None)
        config = launch.files["/home/agent/.hermes/config.yaml"]
        self.assertIn(checkout_path(self.session), config)
        self.assertNotIn("/workspaces/shared/", config)

    def test_unverified_volume_is_never_removed(self):
        runtime = Mock()
        volume = runtime._client.return_value.volumes.get.return_value
        volume.attrs = {"Labels": {"agentcc.managed": "false"}}
        helper = WorktreeRuntime(runtime)
        with self.assertRaisesRegex(RuntimeUnavailable, "unverified"):
            helper.remove_repository_storage(self.workspace.id)
        volume.remove.assert_not_called()

    def test_removed_branch_preview_does_not_recreate_checkout_volume(self):
        runtime = Mock()
        helper = WorktreeRuntime(runtime)
        task = Worktree(workspace_id=self.workspace.id, name="Removed", branch="agentcc/task",
                        base_commit="a" * 40, state="removed", tip="b" * 40)
        client = runtime._client.return_value
        client.volumes.get.return_value.attrs = {"Labels": helper.labels(self.workspace.id)}
        with patch.object(helper, "_run", return_value=b'{"result":{"tip":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","dirty":false}}') as run:
            result = helper.run("branch", self.workspace, task)
        self.assertEqual(result["tip"], task.tip)
        self.assertEqual(set(run.call_args.args[2]), {repository_volume(self.workspace.id)})
        client.volumes.create.assert_not_called()

    def test_nonempty_storage_is_never_deleted(self):
        runtime = Mock()
        task = Worktree(workspace_id=self.workspace.id, name="Test", branch="agentcc/test", base_commit="a" * 40)
        helper = WorktreeRuntime(runtime)
        volume = runtime._client.return_value.volumes.get.return_value
        volume.attrs = {"Labels": helper.labels(self.workspace.id, task.id)}
        with patch.object(helper, "_run", return_value=b'{"empty": false}'):
            with self.assertRaisesRegex(ValueError, "still contains files"):
                helper.remove_checkout_storage(task)
        volume.remove.assert_not_called()

    def test_stale_host_volume_refresh_never_forces_an_attached_volume(self):
        runtime = Mock(workspace_host_roots=("/approved",))
        helper = WorktreeRuntime(runtime)
        task = Worktree(workspace_id=self.workspace.id, name="Test", branch="agentcc/test", base_commit="a" * 40)
        task.host_path = f"/approved/agentcc-task-{task.id}"
        volume = runtime._client.return_value.volumes.get.return_value
        volume.attrs = {"Labels": helper.labels(self.workspace.id, task.id),
                        "Options": {"type": "none", "o": "bind", "device": task.host_path}}
        volume.remove.side_effect = APIError("volume is in use")
        with patch.object(helper, "_ensure_asset") as create:
            with self.assertRaises(APIError):
                helper._refresh_stale_task_volume(task)
            volume.remove.assert_called_once_with(force=False)
            create.assert_not_called()
