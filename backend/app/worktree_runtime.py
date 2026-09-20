"""Confined Docker helpers for repository and durable checkout storage."""

import json
import logging
import os
from functools import wraps
from pathlib import Path

from docker.errors import DockerException
from requests.exceptions import RequestException

from .checkout import repository_root, repository_volume, worktree_root, worktree_volume
from .models import Workspace, Worktree
from .runtime import RuntimeUnavailable


def storage_errors(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (DockerException, RequestException) as error:
            raise RuntimeUnavailable("Repository storage is unavailable or still attached to a container. Retained files were not force-deleted.") from error
    return wrapped


class WorktreeRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
        self.image = os.getenv("AGENTCC_GIT_HELPER_IMAGE", "agentcc-git-helper:dev")
        self.program = Path(__file__).with_name("git_helper.py").read_text()

    def _verify(self, client, name, labels):
        volume = client.volumes.get(name)
        actual = volume.attrs.get("Labels") or {}
        if any(actual.get(key) != value for key, value in labels.items()):
            raise RuntimeUnavailable("Refusing to access unverified repository storage.")
        return volume

    @staticmethod
    def labels(workspace_id, worktree_id=None):
        labels = {"agentcc.managed": "true", "agentcc.kind": "worktree" if worktree_id else "repository",
                  "agentcc.workspace_id": str(workspace_id)}
        if worktree_id:
            labels["agentcc.worktree_id"] = str(worktree_id)
        return labels

    def _run(self, client, command, volumes, *, root=False):
        container = None
        try:
            container = client.containers.run(
                self.image, entrypoint=["python3"], command=command, detach=True,
                user="0" if root else "10001:10001", volumes=volumes, network_disabled=True,
                read_only=True, cap_drop=["ALL"], cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER"] if root else [],
                security_opt=["no-new-privileges:true"], tmpfs={"/tmp": "rw,nosuid,nodev,size=64m"},
                mem_limit="256m", pids_limit=64,
                labels={"agentcc.managed": "true", "agentcc.kind": "git-helper"},
            )
            result = container.wait(timeout=180)
            data = container.logs(stdout=True, stderr=False)
            if result["StatusCode"] != 0 and root:
                raise RuntimeUnavailable("Could not prepare the repository storage permissions.")
            return data
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable("Git helper unavailable. Build the configured agentcc-git-helper image and verify Docker storage access.") from error
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    logging.getLogger(__name__).warning("Could not remove finished Git helper %s", container.id, exc_info=True)

    def _ensure_asset(self, client, name, labels, host_path=None):
        from docker.errors import NotFound
        try:
            volume = self._verify(client, name, labels)
        except NotFound:
            volume = None
        options = {}
        if host_path:
            root, child = os.path.dirname(host_path), os.path.basename(host_path)
            if root not in self.runtime.workspace_host_roots or child != f"agentcc-task-{labels.get('agentcc.worktree_id')}":
                raise RuntimeUnavailable("Task host path is outside approved storage.")
            self._run(client, ["-c", "import os,sys; p='/host/'+sys.argv[1]; os.mkdir(p,0o2775) if not os.path.exists(p) else None; assert not os.path.islink(p); os.chown(p,10001,10001); os.chmod(p,0o2775)", child],
                      {root: {"bind": "/host", "mode": "rw"}}, root=True)
            options = {"type": "none", "o": "bind", "device": host_path}
        if volume is None:
            client.volumes.create(name=name, labels=labels, driver_opts=options)
        elif (volume.attrs.get("Options") or {}) != options:
            raise RuntimeUnavailable("Repository storage mount does not match its recorded location.")
        volume = self._verify(client, name, labels)
        self._run(client, ["-c", "import os; os.chown('/asset',10001,10001); os.chmod('/asset',0o2775)"],
                  {name: {"bind": "/asset", "mode": "rw"}}, root=True)
        return volume

    def run(self, action: str, workspace: Workspace, task: Worktree | None = None, **options):
        try:
            try:
                return self._run_action(action, workspace, task, **options)
            except RuntimeUnavailable as error:
                cause = error.__cause__ or error
                if self.runtime._is_stale_host_workspace_mount(cause, workspace):
                    self.runtime._refresh_stale_workspace_volume(self.runtime._client(), workspace)
                elif task and task.host_path and "failed to populate volume" in str(cause).lower() and worktree_volume(task.id) in str(cause):
                    self._refresh_stale_task_volume(task)
                else:
                    raise
                return self._run_action(action, workspace, task, **options)
        except (ValueError, RuntimeUnavailable):
            raise
        except Exception as error:
            raise RuntimeUnavailable("Repository storage is unavailable. Verify its Docker volumes and approved host location.") from error

    def _refresh_stale_task_volume(self, task):
        root, child = os.path.dirname(task.host_path), os.path.basename(task.host_path)
        if root not in self.runtime.workspace_host_roots or child != f"agentcc-task-{task.id}":
            raise RuntimeUnavailable("Refusing to refresh an unverified task host path.")
        client = self.runtime._client()
        name, labels = worktree_volume(task.id), self.labels(task.workspace_id, task.id)
        volume = self._verify(client, name, labels)
        if (volume.attrs.get("Options") or {}) != {"type": "none", "o": "bind", "device": task.host_path}:
            raise RuntimeUnavailable("Refusing to refresh an unverified task mount.")
        volume.remove(force=False)
        self._ensure_asset(client, name, labels, task.host_path)

    def _run_action(self, action: str, workspace: Workspace, task: Worktree | None = None, **options):
        from docker.errors import NotFound
        client = self.runtime._client()
        volumes = {}
        payload = {"action": action, "source": "/source", **options}
        if action in {"probe", "create", "export"}:
            self._verify(client, workspace.volume_name, self.runtime._workspace_volume_labels(workspace))
            volumes[workspace.volume_name] = {"bind": "/source", "mode": "rw" if action == "export" else "ro"}
        if task:
            repo_name, task_name = repository_volume(workspace.id), worktree_volume(task.id)
            needs_checkout = action != "branch" and (action != "export" or task.state != "removed")
            if action == "create":
                self._ensure_asset(client, repo_name, self.labels(workspace.id))
                self._ensure_asset(client, task_name, self.labels(workspace.id, task.id), task.host_path)
            elif action != "remove":
                self._verify(client, repo_name, self.labels(workspace.id))
                if needs_checkout:
                    self._verify(client, task_name, self.labels(workspace.id, task.id))
            volumes[repo_name] = {"bind": repository_root(workspace.id), "mode": "rw"}
            if needs_checkout:
                volumes[task_name] = {"bind": worktree_root(task.id), "mode": "rw"}
            if action == "remove":
                # Failed creation may have allocated neither or only one asset.
                # Never let Docker implicitly create a missing recovery volume.
                for name, labels in [(repo_name, self.labels(workspace.id)), (task_name, self.labels(workspace.id, task.id))]:
                    try:
                        self._verify(client, name, labels)
                    except NotFound:
                        volumes.pop(name, None)
            payload.update(id=str(task.id), repo=f"{repository_root(workspace.id)}/repo.git",
                           checkout=f"{worktree_root(task.id)}/checkout", branch=task.branch,
                           base_commit=task.base_commit, source_branch=task.source_branch,
                           exported_commit=task.exported_commit)
        data = self._run(client, ["-c", self.program, json.dumps(payload)], volumes)
        try:
            result = json.loads(data)
        except (ValueError, TypeError) as error:
            raise RuntimeUnavailable("Git helper returned an invalid response.") from error
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]

    @storage_errors
    def remove_checkout_storage(self, task: Worktree):
        from docker.errors import NotFound
        client = self.runtime._client()
        try:
            volume = self._verify(client, worktree_volume(task.id), self.labels(task.workspace_id, task.id))
            data = self._run(client, ["-c", "import os,json; print(json.dumps({'empty': not os.listdir('/asset')}))"],
                             {volume.name: {"bind": "/asset", "mode": "ro"}})
            if not json.loads(data).get("empty"):
                raise ValueError("Task storage still contains files. Review them before removing it.")
            volume.remove(force=False)
        except NotFound:
            pass
        if task.host_path:
            root, child = os.path.dirname(task.host_path), os.path.basename(task.host_path)
            if root not in self.runtime.workspace_host_roots or child != f"agentcc-task-{task.id}":
                raise RuntimeUnavailable("Refusing to remove unverified task storage.")
            self._run(client, ["-c", "import os,sys; p='/host/'+sys.argv[1]; os.rmdir(p) if os.path.exists(p) else None", child],
                      {root: {"bind": "/host", "mode": "rw"}}, root=True)

    @storage_errors
    def remove_repository_storage(self, workspace_id):
        from docker.errors import NotFound
        client = self.runtime._client()
        try:
            self._verify(client, repository_volume(workspace_id), self.labels(workspace_id)).remove(force=False)
        except NotFound:
            pass
