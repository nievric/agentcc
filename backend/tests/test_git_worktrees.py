from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from support import git, output, seed
from app.git_helper import GitError, execute, probe, valid_branch


class GitWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentcc-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.base = seed(self.root)
        self.repo = self.root / "repo.git"

    def task(self, branch="agentcc/test"):
        identifier = str(uuid4())
        return dict(id=identifier, source=str(self.source), repo=str(self.repo),
                    checkout=str(self.root / identifier / "checkout"), branch=branch,
                    base_commit=self.base, source_branch="main")

    def commit(self, path):
        git(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-am", "Task change")

    def test_two_tasks_do_not_share_edits_or_source_files(self):
        a, b = self.task("agentcc/a"), self.task("agentcc/b")
        execute({**a, "action": "create"})
        execute({**b, "action": "create"})
        (Path(a["checkout"]) / "file.txt").write_text("A\n")
        self.commit(a["checkout"])
        self.assertEqual((Path(b["checkout"]) / "file.txt").read_text(), "original\n")
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        self.assertEqual(output(self.source, "rev-parse", "HEAD"), self.base)
        self.assertEqual(output(a["checkout"], "rev-parse", "--git-common-dir"), str(self.repo))

    def test_dirty_source_requires_acknowledgement_and_is_not_copied(self):
        (self.source / "local.txt").write_text("unsaved")
        task = self.task()
        with self.assertRaisesRegex(GitError, "local edits"):
            execute({**task, "action": "create"})
        execute({**task, "action": "create", "use_committed_version": True})
        self.assertFalse((Path(task["checkout"]) / "local.txt").exists())
        self.assertEqual((self.source / "local.txt").read_text(), "unsaved")

    def test_changed_base_is_rejected(self):
        task = self.task()
        (self.source / "file.txt").write_text("new\n")
        self.commit(self.source)
        with self.assertRaisesRegex(GitError, "starting branch changed"):
            execute({**task, "action": "create"})

    def test_retry_preserves_existing_dirty_checkout(self):
        task = self.task()
        execute({**task, "action": "create"})
        (Path(task["checkout"]) / "file.txt").write_text("keep my work")
        self.assertTrue(execute({**task, "action": "create"})["dirty"])
        self.assertEqual((Path(task["checkout"]) / "file.txt").read_text(), "keep my work")

    def test_export_checks_reviewed_tip_and_does_not_change_source_checkout(self):
        task = self.task()
        execute({**task, "action": "create"})
        (Path(task["checkout"]) / "file.txt").write_text("task\n")
        self.commit(task["checkout"])
        tip = output(task["checkout"], "rev-parse", "HEAD")
        with self.assertRaisesRegex(GitError, "changed after review"):
            execute({**task, "action": "export", "expected_tip": self.base})
        execute({**task, "action": "export", "expected_tip": tip})
        self.assertEqual(output(self.source, "rev-parse", task["branch"]), tip)
        self.assertEqual(output(self.source, "rev-parse", "HEAD"), self.base)
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")

    def test_export_does_not_overwrite_existing_branch(self):
        task = self.task()
        execute({**task, "action": "create"})
        (Path(task["checkout"]) / "file.txt").write_text("task\n")
        self.commit(task["checkout"])
        git(self.source, "branch", task["branch"])
        with self.assertRaisesRegex(GitError, "already exists"):
            execute({**task, "action": "export", "expected_tip": output(task["checkout"], "rev-parse", "HEAD")})

    def test_remove_refuses_untracked_and_ignored_files_and_keeps_branch(self):
        task = self.task()
        execute({**task, "action": "create"})
        for filename in ("untracked.txt", ".env"):
            path = Path(task["checkout"]) / filename
            path.write_text("keep")
            with self.assertRaisesRegex(GitError, "ignored files"):
                execute({**task, "action": "remove"})
            self.assertTrue(path.exists())
            path.unlink()
        execute({**task, "action": "remove"})
        self.assertTrue(Path(task["checkout"]).parent.exists())
        self.assertEqual(output(self.repo, "rev-parse", task["branch"]), self.base)
        self.assertEqual(execute({**task, "action": "branch"})["tip"], self.base)

    def test_unsupported_layout_and_unsafe_branch_names(self):
        (self.source / ".gitmodules").write_text("submodules")
        self.assertFalse(probe(self.source)["available"])
        for branch in ("--force", "../../main", "@{-1}", "HEAD", "a b", "a..b"):
            with self.subTest(branch=branch), self.assertRaises(GitError):
                valid_branch(branch)

    def test_missing_sibling_is_locked_against_pruning(self):
        task = self.task()
        execute({**task, "action": "create"})
        path = Path(task["checkout"])
        path.rename(path.with_name("unmounted"))
        git(self.repo, "worktree", "prune", "--expire", "now")
        self.assertIn(task["branch"], output(self.repo, "worktree", "list", "--porcelain"))

    def test_cleanup_refuses_files_outside_checkout_and_is_idempotent(self):
        task = self.task()
        execute({**task, "action": "create"})
        extra = Path(task["checkout"]).parent / "notes.txt"
        extra.write_text("keep")
        with self.assertRaisesRegex(GitError, "outside its checkout"):
            execute({**task, "action": "remove"})
        extra.unlink()
        execute({**task, "action": "remove"})
        self.assertEqual(execute({**task, "action": "remove"})["tip"], self.base)

    def test_commit_identity_is_only_copied_with_consent(self):
        task = self.task()
        git(self.source, "config", "remote.origin.url", "https://example.invalid/private")
        execute({**task, "action": "create"})
        self.assertNotEqual(git(self.repo, "config", "--get", "user.name", check=False).returncode, 0)
        second = self.task("agentcc/identity")
        execute({**second, "action": "create", "reuse_git_identity": True})
        self.assertEqual(output(self.repo, "config", "user.name"), "Test Author")
        self.assertEqual(output(self.repo, "config", "user.email"), "tests@example.invalid")
        self.assertNotEqual(git(self.repo, "config", "--get", "remote.origin.url", check=False).returncode, 0)

    def test_repeat_export_only_allows_known_fast_forward_branch(self):
        task = self.task()
        execute({**task, "action": "create"})
        (Path(task["checkout"]) / "file.txt").write_text("first\n")
        self.commit(task["checkout"])
        first = output(task["checkout"], "rev-parse", "HEAD")
        execute({**task, "action": "export", "expected_tip": first})
        # Lost export response: retry succeeds without rewriting the ref.
        execute({**task, "action": "export", "expected_tip": first})
        (Path(task["checkout"]) / "file.txt").write_text("second\n")
        self.commit(task["checkout"])
        second = output(task["checkout"], "rev-parse", "HEAD")
        execute({**task, "action": "export", "expected_tip": second, "exported_commit": first})
        git(task["checkout"], "reset", "--hard", first)
        with self.assertRaisesRegex(GitError, "rewrite"):
            execute({**task, "action": "export", "expected_tip": first, "exported_commit": second})
        self.assertEqual(output(self.source, "rev-parse", task["branch"]), second)

    def test_export_refuses_a_branch_checked_out_in_source(self):
        task = self.task()
        execute({**task, "action": "create"})
        execute({**task, "action": "export", "expected_tip": self.base})
        git(self.source, "checkout", task["branch"])
        with self.assertRaisesRegex(GitError, "checked out"):
            execute({**task, "action": "export", "expected_tip": self.base, "exported_commit": self.base})
