from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from support import FakeRuntime, LocalGitRuntime, make_store, make_workspace, seed
from app.models import CheckoutRequest, SessionAction, SessionCreate
from app.worktrees import WorktreeService


class DeferredExecutor:
    def submit(self, *args):
        pass


class WorktreeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentcc-service-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = make_store(root / "data")
        self.workspace = make_workspace(self.store)
        self.source, self.base = seed(root)
        self.runtime = FakeRuntime()
        self.service = WorktreeService(self.store, self.runtime, LocalGitRuntime(root, self.source))
        self.service.executor.shutdown()
        self.service.executor = DeferredExecutor()
        self.payload = SessionCreate(name="Fix issue", workspace_id=self.workspace.id,
                                     checkout=CheckoutRequest(mode="new_worktree", source_branch="main", expected_base_commit=self.base))

    def launch(self):
        op = self.service.launch(self.payload, str(uuid4()))
        self.service.run_operation(op.id)
        result = self.store.get_operation(op.id)
        self.assertEqual(result.state, "completed", result.error)
        return result, self.store.get_worktree(op.worktree_id)

    def test_launch_idempotency_and_payload_mismatch(self):
        a = self.service.launch(self.payload, "same")
        b = self.service.launch(self.payload, "same")
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(self.store.list_worktrees(self.workspace.id)), 1)
        changed = self.payload.model_copy(update={"name": "Different"})
        with self.assertRaisesRegex(ValueError, "different request"):
            self.service.launch(changed, "same")
        self.service.run_operation(a.id)
        self.service.run_operation(a.id)
        self.assertEqual(len(self.runtime.provisions), 1)

    def test_single_writer_and_stop_then_continue(self):
        op, task = self.launch()
        reuse = self.payload.model_copy(update={"checkout": CheckoutRequest(mode="existing_worktree", worktree_id=task.id)})
        with self.assertRaisesRegex(ValueError, "active"):
            self.service.launch(reuse, None)
        self.store.act_on_session(op.session_id, SessionAction(action="suspend"))
        with self.assertRaisesRegex(ValueError, "active"):
            self.service.launch(reuse, None)
        self.store.act_on_session(op.session_id, SessionAction(action="stop"))
        self.store.release_worktree(task.id, op.session_id)
        session = self.service.launch(reuse, None)
        self.assertEqual(session.worktree_id, task.id)

    def test_concurrent_reservations_have_only_one_winner(self):
        op, task = self.launch()
        self.store.act_on_session(op.session_id, SessionAction(action="stop"))
        self.store.release_worktree(task.id, op.session_id)
        def reserve(_):
            try:
                self.store.reserve_worktree(task.id, uuid4())
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(reserve, range(5))), 1)

    def test_session_deletion_preserves_checkout_and_archive_restore(self):
        op, task = self.launch()
        with self.assertRaises(ValueError):
            self.store.soft_delete_workspace(self.workspace.id)
        self.store.act_on_session(op.session_id, SessionAction(action="stop"))
        self.store.release_worktree(task.id, op.session_id)
        self.store.delete_session(op.session_id)
        self.assertTrue((self.service.git.root / str(task.id) / "checkout/file.txt").exists())
        self.assertEqual(self.service.task_action(task.id, "archive").state, "archived")
        self.assertEqual(self.service.task_action(task.id, "restore").state, "ready")
        with self.assertRaisesRegex(ValueError, "retained branch"):
            self.store.guard_worktree_assets(self.workspace.id, hard=True)

    def test_recovery_after_git_creation_does_not_duplicate_checkout(self):
        op = self.service.launch(self.payload, "recovery")
        task = self.store.get_worktree(op.worktree_id)
        self.service.git.run("create", self.workspace, task)
        reopened = make_store(Path(self.temp.name) / "data")
        self.service.store = reopened
        self.service.run_operation(op.id)
        self.assertEqual(reopened.get_operation(op.id).state, "completed")
        self.assertEqual(len(reopened.list_worktrees(self.workspace.id)), 1)

    def test_legacy_launch_uses_original_workspace(self):
        payload = SessionCreate(name="Legacy", workspace_id=self.workspace.id)
        session = self.service.launch(payload, None)
        self.assertIsNone(session.worktree_id)
        self.assertEqual(self.store.list_worktrees(self.workspace.id), [])

    def test_failed_creation_can_be_cancelled_and_removed_without_losing_source_edits(self):
        (self.source / "local.txt").write_text("do not copy or delete")
        op = self.service.launch(self.payload, "dirty-failure")
        self.service.run_operation(op.id)
        self.assertEqual(self.store.get_operation(op.id).state, "failed")
        self.service.cancel_pending(op.worktree_id)
        self.assertEqual(self.service.task_action(op.worktree_id, "remove").state, "removed")
        self.assertEqual((self.source / "local.txt").read_text(), "do not copy or delete")

    def test_removal_recovers_after_git_and_storage_response_loss(self):
        op, task = self.launch()
        self.store.act_on_session(op.session_id, SessionAction(action="stop"))
        self.store.release_worktree(task.id, op.session_id)
        self.store.delete_session(op.session_id)
        self.service.git.run("remove", self.workspace, task)
        with patch.object(self.service.git, "remove_checkout_storage", side_effect=RuntimeError("storage down")):
            with self.assertRaises(RuntimeError):
                self.service.task_action(task.id, "remove")
        self.assertEqual(self.store.get_worktree(task.id).state, "removing")
        self.assertEqual(self.service.task_action(task.id, "remove").state, "removed")

    def test_recovery_releases_completed_reservation(self):
        op, task = self.launch()
        self.store.act_on_session(op.session_id, SessionAction(action="stop"))
        self.service.recover()
        self.assertIsNone(self.store.get_worktree(task.id).reserved_session_id)

    def test_failed_continuation_retains_new_reservation_until_cancelled(self):
        op, task = self.launch()
        self.store.act_on_session(op.session_id, SessionAction(action="stop"))
        self.store.release_worktree(task.id, op.session_id)
        reuse = self.payload.model_copy(update={"checkout": CheckoutRequest(mode="existing_worktree", worktree_id=task.id)})
        with patch.object(self.runtime, "provision", side_effect=RuntimeError("lost response")):
            with self.assertRaises(RuntimeError):
                self.service.launch(reuse, None)
        retained = self.store.get_worktree(task.id)
        self.assertIsNotNone(retained.reserved_session_id)
        self.assertIsNone(retained.operation_id)
        self.service.cancel_pending(task.id)
        self.assertIsNone(self.store.get_worktree(task.id).reserved_session_id)
