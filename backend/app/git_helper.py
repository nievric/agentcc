"""Stdlib-only Git operations, executed as an unprivileged container user.

Also runnable against disposable local repositories in tests. The controller owns
all paths; no browser-supplied command or path is evaluated here.
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


class GitError(ValueError):
    pass


def git(path, *args, check=True, trusted_paths=()):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
    command = ["git", "-c", f"safe.directory={path}", "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=always"]
    for trusted in trusted_paths:
        command.extend(["-c", f"safe.directory={trusted}"])
    command.extend(["-C", str(path), *args])
    result = subprocess.run(command, env=env, capture_output=True, timeout=120)
    if len(result.stdout) > 2_000_000:
        raise GitError("Repository output exceeds the supported size.")
    if check and result.returncode:
        raise GitError(result.stderr.decode("utf-8", errors="replace")[-1500:].strip() or "Git operation failed.")
    return result


def output(path, *args):
    return git(path, *args).stdout.decode("utf-8", errors="replace").strip()


def valid_branch(branch, *, check_with_git=True):
    # Disallow revision shortcuts and option-like names even before invoking Git.
    if (not branch or len(branch) > 200 or branch.startswith(("-", "/")) or branch.endswith(("/", "."))
        or "@{" in branch or ".." in branch or branch in {"HEAD", "@"}
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch)
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))):
        raise GitError("Choose a valid local branch name.")
    if check_with_git and subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"], capture_output=True).returncode:
        raise GitError("Choose a valid local branch name.")
    return branch


def probe(source):
    source = Path(source)
    dotgit = source / ".git"
    if not dotgit.is_dir() or dotgit.is_symlink():
        return {"available": False, "reason": "Separate branches require a Git repository at the workspace root with its own .git directory."}
    if (dotgit / "objects/info/alternates").exists() or (dotgit / "shallow").exists():
        return {"available": False, "reason": "Shallow repositories and shared object alternates are not supported yet."}
    if (source / ".gitmodules").exists() or (dotgit / "info/sparse-checkout").exists():
        return {"available": False, "reason": "Submodules and sparse checkouts are not supported yet."}
    if git(source, "config", "--get-regexp", r"^(filter\.|extensions\.|remote\..*\.promisor|core\.sparsecheckout)", check=False).returncode == 0:
        return {"available": False, "reason": "This repository uses extensions or checkout filters that are not supported yet."}
    head = git(source, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    if head.returncode:
        return {"available": False, "reason": "Create an initial commit in VS Code before starting a separate branch."}
    branches = {}
    for line in output(source, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines():
        name, commit = line.rsplit(" ", 1)
        branches[name] = commit
    branch = git(source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.decode().strip() or None
    return {"available": True, "head": head.stdout.decode().strip(), "branch": branch, "branches": branches,
            "author_name": git(source, "config", "--local", "--get", "user.name", check=False).stdout.decode().strip() or None,
            "author_email": git(source, "config", "--local", "--get", "user.email", check=False).stdout.decode().strip() or None,
            "dirty": bool(git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout)}


def inspect(repo, checkout, branch):
    checkout = Path(checkout)
    if not checkout.is_dir() or checkout.is_symlink() or not (checkout / ".git").is_file() or (checkout / ".git").is_symlink():
        raise GitError("Task checkout is missing; its files have been retained for recovery.")
    common = Path(output(checkout, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = checkout / common
    if common.resolve() != Path(repo).resolve():
        raise GitError("Task checkout points to an unexpected repository.")
    if output(checkout, "symbolic-ref", "--short", "HEAD") != branch:
        raise GitError("The task's branch was changed outside AgentCC. Restore it before continuing.")
    # Include ignored files: Git's normal clean check would discard them.
    dirty = bool(git(checkout, "status", "--porcelain=v1", "--untracked-files=all", "--ignored").stdout)
    return {"tip": output(checkout, "rev-parse", "HEAD"), "dirty": dirty}


def execute(payload):
    action = payload["action"]
    source = Path(payload.get("source", "/source"))
    if action == "probe":
        return probe(source)
    repo = Path(payload["repo"])
    checkout = Path(payload["checkout"])
    branch = valid_branch(payload["branch"])
    base = payload["base_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise GitError("Invalid starting commit.")
    if action == "create":
        if checkout.exists():
            # Safe retry after a helper/HTTP response was lost.
            return inspect(repo, checkout, branch)
        info = probe(source)
        if not info.get("available"):
            raise GitError(info["reason"])
        selected = info["branches"].get(payload.get("source_branch")) if payload.get("source_branch") else info["head"]
        if selected != base:
            raise GitError("The starting branch changed. Refresh the launch preview and try again.")
        if info["dirty"] and not payload.get("use_committed_version"):
            raise GitError("The original workspace has local edits. Choose Use committed version to exclude those edits.")
        repo.parent.mkdir(parents=True, exist_ok=True)
        if not repo.exists():
            git(repo.parent, "init", "--bare", "--shared=group", str(repo))
        if repo.is_symlink() or output(repo, "rev-parse", "--is-bare-repository") != "true":
            raise GitError("Managed repository storage is invalid.")
        if payload.get("reuse_git_identity"):
            if not info["author_name"] or not info["author_email"]:
                raise GitError("Configure both user.name and user.email in the original repository before reusing its commit author.")
            git(repo, "config", "user.name", info["author_name"])
            git(repo, "config", "user.email", info["author_email"])
        # Local transport copies objects and never creates object alternates.
        upload = shlex.join(["git", "-c", f"safe.directory={source / '.git'}", "-c", "core.hooksPath=/dev/null", "upload-pack"])
        git(repo, "fetch", f"--upload-pack={upload}", "--no-tags", "--no-write-fetch-head", str(source), f"{base}:refs/agentcc/bases/{payload['id']}")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--lock", "--reason", "AgentCC managed checkout", "-b", branch, str(checkout), base)
        return inspect(repo, checkout, branch)
    if action == "inspect":
        return inspect(repo, checkout, branch)
    if action == "branch":
        return {"tip": output(repo, "rev-parse", "--verify", f"refs/heads/{branch}"), "dirty": False}
    if action == "export":
        if not probe(source).get("available"):
            raise GitError("The original repository is no longer available in its supported layout.")
        tip = output(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        if tip != payload["expected_tip"]:
            raise GitError("The task changed after review. Refresh it before exporting.")
        ref = f"refs/heads/{branch}"
        if f"branch {ref}" in output(source, "worktree", "list", "--porcelain").splitlines():
            raise GitError("The destination branch is checked out. Switch away from it before exporting.")
        old = git(source, "rev-parse", "--verify", ref, check=False).stdout.decode().strip()
        if old == tip:
            return {"tip": tip, "exported_commit": tip}
        if old != (payload.get("exported_commit") or ""):
            raise GitError("The destination branch already exists or changed outside AgentCC; it was not overwritten.")
        git(source, "fetch", "--no-tags", "--no-write-fetch-head", str(repo), tip, trusted_paths=[repo])
        if old and git(source, "merge-base", "--is-ancestor", old, tip, check=False).returncode:
            raise GitError("Export would rewrite the destination branch; it was not overwritten.")
        git(source, "update-ref", ref, tip, old or "0" * 40)
        return {"tip": tip, "exported_commit": tip}
    if action == "remove":
        if checkout.parent.exists() and any(item.name != "checkout" for item in checkout.parent.iterdir()):
            raise GitError("Task storage contains files outside its checkout. Review and remove those files before cleanup.")
        if not checkout.exists() and not checkout.is_symlink():
            if not repo.exists():
                return {"tip": None, "dirty": False}
            registered = output(repo, "worktree", "list", "--porcelain").splitlines()
            if f"worktree {checkout}" in registered:
                raise GitError("The registered checkout is missing. Restore its storage before removing it.")
            # Git removal completed before the controller saved its result.
            tip = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False).stdout.decode().strip()
            return {"tip": tip or None, "dirty": False}
        info = inspect(repo, checkout, branch)
        if info["dirty"]:
            raise GitError("This task contains modified, untracked, or ignored files. Review and clean them in VS Code before removing it.")
        git(repo, "worktree", "unlock", str(checkout))
        try:
            git(repo, "worktree", "remove", str(checkout))
        except Exception:
            git(repo, "worktree", "lock", "--reason", "AgentCC managed checkout", str(checkout), check=False)
            raise
        return info
    raise GitError("Unknown repository operation.")


if __name__ == "__main__":
    os.umask(0o002)
    try:
        print(json.dumps({"result": execute(json.loads(sys.argv[1]))}))
    except (GitError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
