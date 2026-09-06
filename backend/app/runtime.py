"""Narrow Docker Engine boundary for local AgentCC session containers."""

from __future__ import annotations

import os
import io
import logging
import tarfile
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .harnesses import HarnessConfigurationError, JournalTelemetryProbe, KiloSqliteTelemetryProbe, SqliteTelemetryProbe, harnesses
from .models import AgentActivity, RegisteredModel, Session, SessionState, Workspace


logger = logging.getLogger(__name__)


class RuntimeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProvisionedContainer:
    container_id: str
    volume_name: str


@dataclass
class TerminalPty:
    """A Docker exec TTY backed by a single container socket."""

    stream: Any
    client: Any
    exec_id: str
    created_harness: bool = False

    def read(self, size: int = 4096) -> bytes:
        socket = getattr(self.stream, "_sock", self.stream)
        reader = getattr(socket, "recv", None) or getattr(socket, "read")
        return reader(size)

    def write(self, data: bytes) -> None:
        socket = getattr(self.stream, "_sock", self.stream)
        writer = getattr(socket, "sendall", None) or getattr(socket, "send") or getattr(socket, "write")
        writer(data)

    def resize(self, columns: int, rows: int) -> None:
        self.client.api.exec_resize(self.exec_id, height=rows, width=columns)

    def close(self) -> None:
        # Docker SDK wraps the hijacked connection in an HTTP response. Closing
        # the raw socket avoids response-buffer flushing on a PTY stream.
        socket = getattr(self.stream, "_sock", None)
        closer = getattr(socket, "close", None) or getattr(self.stream, "close", None)
        if closer:
            try:
                closer()
            except Exception:
                pass


@dataclass(frozen=True)
class TerminalSnapshot:
    """A bounded, non-interactive view of the detached harness terminal."""

    text: str
    agent_activity: AgentActivity


class DockerContainerRuntime:
    """Creates only AgentCC-labelled, unexposed session containers.

    Docker socket access is highly privileged. This service deliberately
    presents a narrow, auditable surface; callers never pass an image, command,
    mounts, network, or privileged flags from a browser request.
    """

    def __init__(self) -> None:
        self.enabled = os.getenv("AGENTCC_RUNTIME_ENABLED", "false").lower() == "true"
        self.image = os.getenv("AGENTCC_SESSION_IMAGE", "agentcc-session-codex:dev")
        self.network_name = os.getenv("AGENTCC_SESSION_NETWORK", "agentcc-sessions")
        self.memory_limit = os.getenv("AGENTCC_SESSION_MEMORY", "2g")
        self.nano_cpus = int(os.getenv("AGENTCC_SESSION_NANO_CPUS", "2000000000"))
        self.pids_limit = int(os.getenv("AGENTCC_SESSION_PIDS", "256"))
        roots = [os.path.normpath(path.strip()) for path in os.getenv("AGENTCC_WORKSPACE_HOST_ROOTS", "").split(",") if path.strip()]
        if any(not os.path.isabs(path) or path == "/" for path in roots):
            raise RuntimeUnavailable("AGENTCC_WORKSPACE_HOST_ROOTS must contain non-root absolute host paths")
        self.workspace_host_roots = tuple(dict.fromkeys(roots))
        self._workspace_usage_cache: dict[str, tuple[float, int, int]] = {}

    @property
    def default_workspace_host_root(self) -> str | None:
        return self.workspace_host_roots[0] if self.workspace_host_roots else None

    def workspace_host_path(self, folder_name: str, host_root: str | None) -> str | None:
        if host_root is None:
            return None
        normalized = os.path.normpath(host_root)
        if normalized not in self.workspace_host_roots:
            raise RuntimeUnavailable("workspace host root is not approved by the local runtime configuration")
        if not folder_name or "/" in folder_name or folder_name in {".", ".."}:
            raise RuntimeUnavailable("workspace folder name is invalid")
        return os.path.join(normalized, folder_name)

    def _client(self):
        if not self.enabled:
            raise RuntimeUnavailable("container runtime is disabled")
        try:
            import docker
            client = docker.from_env()
            client.ping()
            return client
        except Exception as error:  # Docker SDK exposes several transport errors.
            raise RuntimeUnavailable("Docker Engine is unavailable to the API service") from error

    def _network(self, client):
        try:
            return client.networks.get(self.network_name)
        except Exception:
            return client.networks.create(
                self.network_name,
                driver="bridge",
                internal=False,
                labels={"agentcc.managed": "true", "agentcc.kind": "session-network"},
            )

    def _connect_control_plane(self, network) -> None:
        """Join this API container to the private session network when hosted."""
        hostname = os.getenv("HOSTNAME")
        if not hostname:
            return
        try:
            network.connect(hostname)
        except Exception:
            # It is normal for a re-used network to already include the API.
            pass

    def create_workspace_volume(self, workspace: Workspace) -> None:
        """Create the durable asset store independently of any session."""
        client = self._client()
        try:
            client.volumes.get(workspace.volume_name)
        except Exception:
            try:
                self._create_workspace_volume(client, workspace)
            except Exception as error:
                raise RuntimeUnavailable("unable to create the durable workspace storage; verify the configured host root exists and Docker can access it") from error

    @staticmethod
    def _workspace_volume_labels(workspace: Workspace) -> dict[str, str]:
        return {
            "agentcc.managed": "true",
            "agentcc.kind": "shared-workspace",
            "agentcc.workspace_id": str(workspace.id),
            "agentcc.workspace_name": workspace.name,
        }

    def _create_workspace_volume(self, client, workspace: Workspace) -> None:
        labels = self._workspace_volume_labels(workspace)
        if workspace.host_path:
            self._prepare_workspace_directory(client, workspace)
            client.volumes.create(
                name=workspace.volume_name,
                driver="local",
                driver_opts={"type": "none", "o": "bind", "device": workspace.host_path},
                labels={**labels, "agentcc.storage": "host-bind", "agentcc.host_path": workspace.host_path},
            )
        else:
            client.volumes.create(name=workspace.volume_name, labels={**labels, "agentcc.storage": "docker-volume"})

    @staticmethod
    def _is_stale_host_workspace_mount(error: Exception, workspace: Workspace) -> bool:
        """Identify Docker Desktop's expired local bind-volume mount records."""
        detail = str(error).lower()
        return bool(workspace.host_path) and "failed to populate volume" in detail and workspace.volume_name.lower() in detail

    def _refresh_stale_workspace_volume(self, client, workspace: Workspace) -> None:
        """Recreate a host-backed volume whose Docker Desktop bind target vanished.

        The durable data stays at ``workspace.host_path``.  This only removes
        Docker's invalid local-volume reference, and Docker refuses the removal
        if any container still has the volume attached.
        """
        try:
            volume = client.volumes.get(workspace.volume_name)
            labels = volume.attrs.get("Labels") or {}
            expected = self._workspace_volume_labels(workspace)
            if any(labels.get(key) != value for key, value in expected.items()):
                raise RuntimeUnavailable("refusing to refresh an unverified workspace volume")
            volume.remove(force=False)
            self._create_workspace_volume(client, workspace)
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable(
                "unable to refresh the stale workspace bind mount; stop sessions using this workspace and try again"
            ) from error

    @staticmethod
    def _cleanup_unprovisioned_session_volume(client, volume_name: str, session: Session) -> None:
        """Remove the exact disposable volume created for a failed launch."""
        try:
            volume = client.volumes.get(volume_name)
            labels = volume.attrs.get("Labels") or {}
            if (
                labels.get("agentcc.managed") == "true"
                and labels.get("agentcc.kind") == "session-volume"
                and labels.get("agentcc.session_id") == str(session.id)
            ):
                volume.remove(force=False)
        except Exception:
            logger.warning("Could not clean up failed session volume %s", volume_name, exc_info=True)

    def _prepare_workspace_directory(self, client, workspace: Workspace) -> None:
        """Create a fixed UUID child beneath an approved host root via Docker.

        The API never mounts the host directory into itself. This short-lived,
        networkless helper receives only the selected root and a UUID-derived
        child name, avoiding an arbitrary root shell in the session.
        """
        if not workspace.host_path:
            return
        root, child = os.path.dirname(workspace.host_path), os.path.basename(workspace.host_path)
        if root not in self.workspace_host_roots or child != workspace.folder_name:
            raise RuntimeUnavailable("refusing to prepare an unverified workspace host path")
        client.containers.run(
            self.image,
            command=["-d", "-m", "2775", "-o", "1000", "-g", "10001", f"/host/{child}"],
            entrypoint=["/usr/bin/install"],
            user="0",
            volumes={root: {"bind": "/host", "mode": "rw"}},
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            cap_add=["CHOWN", "FOWNER", "DAC_OVERRIDE"],
            security_opt=["no-new-privileges:true"],
            remove=True,
            labels={"agentcc.managed": "true", "agentcc.kind": "workspace-preparer"},
        )

    def workspace_usage(self, workspace: Workspace) -> tuple[int, int] | None:
        """Return a cached, read-only file count and byte total for one workspace.

        The API container never receives a host filesystem mount.  A confined
        helper mounts only this verified workspace volume read-only and emits
        two integers.  Results are intentionally short-lived: the Workspace
        page can refresh useful telemetry without slowing the fleet grid.
        """
        if not self.enabled:
            return None
        cached = self._workspace_usage_cache.get(str(workspace.id))
        if cached is not None and time.monotonic() - cached[0] < 30:
            return cached[1], cached[2]
        client = self._client()
        try:
            output = client.containers.run(
                self.image,
                command=["-c", "printf '%s %s\\n' \"$(find /workspace -xdev -type f -printf . | wc -c)\" \"$(du -sb /workspace | cut -f1)\""],
                entrypoint=["/bin/sh"],
                user="10001:10001",
                volumes={workspace.volume_name: {"bind": "/workspace", "mode": "ro"}},
                network_disabled=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                remove=True,
                labels={"agentcc.managed": "true", "agentcc.kind": "workspace-usage"},
            )
            fields = output.decode("utf-8", errors="replace").strip().split()
            if len(fields) != 2:
                raise ValueError("usage helper produced invalid output")
            file_count, storage_bytes = (int(fields[0]), int(fields[1]))
            if file_count < 0 or storage_bytes < 0:
                raise ValueError("usage helper produced negative values")
            self._workspace_usage_cache[str(workspace.id)] = (time.monotonic(), file_count, storage_bytes)
            return file_count, storage_bytes
        except Exception:
            logger.warning("Could not collect workspace usage for %s", workspace.id, exc_info=True)
            return None

    def provision(self, session: Session, workspace: Workspace, model: RegisteredModel | None = None) -> ProvisionedContainer:
        client = self._client()
        network = self._network(client)
        self._connect_control_plane(network)
        self.create_workspace_volume(workspace)
        short_id = str(session.id).split("-", maxsplit=1)[0]
        volume_name = f"agentcc-session-{short_id}"
        container_name = f"agentcc-session-{short_id}"
        labels = {
            "agentcc.managed": "true",
            "agentcc.kind": "session",
            "agentcc.session_id": str(session.id),
            "agentcc.workspace": session.workspace,
        }
        try:
            client.volumes.create(name=volume_name, labels={**labels, "agentcc.kind": "session-volume"})
            try:
                container = self._run_session_container(client, session, workspace, model, volume_name, container_name)
            except Exception as error:
                if not self._is_stale_host_workspace_mount(error, workspace):
                    raise
                logger.warning("Refreshing stale Docker workspace bind mount for %s", workspace.id)
                self._refresh_stale_workspace_volume(client, workspace)
                container = self._run_session_container(client, session, workspace, model, volume_name, container_name)
        except RuntimeUnavailable:
            self._cleanup_unprovisioned_session_volume(client, volume_name, session)
            raise
        except Exception as error:
            self._cleanup_unprovisioned_session_volume(client, volume_name, session)
            logger.exception("Unable to provision session container for session %s", session.id)
            raise RuntimeUnavailable("unable to provision the isolated session container") from error
        return ProvisionedContainer(container_id=container.id, volume_name=volume_name)

    def _run_session_container(self, client, session: Session, workspace: Workspace, model: RegisteredModel | None, volume_name: str, container_name: str):
        """Run a session container from the harness's current reviewed image."""
        harness = harnesses.for_name(session.harness)
        shared_mount = f"/workspaces/shared/{workspace.folder_name}"
        labels = {
            "agentcc.managed": "true",
            "agentcc.kind": "session",
            "agentcc.session_id": str(session.id),
            "agentcc.workspace": session.workspace,
        }
        return client.containers.run(
            harness.session_image(self.image),
            name=container_name,
            detach=True,
            network=self.network_name,
            labels=labels,
            volumes={
                volume_name: {"bind": "/workspaces/session", "mode": "rw"},
                workspace.volume_name: {"bind": shared_mount, "mode": "rw"},
            },
            environment={"WORKSPACE_ROOT": "/workspaces", "CODE_SERVER_PORT": "8080"},
            working_dir=shared_mount,
            mem_limit=self.memory_limit,
            nano_cpus=self.nano_cpus,
            pids_limit=self.pids_limit,
            cap_drop=["ALL"],
            # The bootstrap entrypoint prepares newly attached volumes and
            # then permanently drops to code-server's unprivileged user.
            cap_add=["CHOWN", "SETGID", "SETUID"],
            security_opt=["no-new-privileges:true"],
            extra_hosts={"host.docker.internal": "host-gateway"} if model is not None and model.provider == "Local gateway" else None,
            # Docker defaults tmpfs mounts to noexec. Kilo's OpenTUI renderer
            # explicitly opts into exec; the other harnesses retain noexec.
            tmpfs={"/tmp": "rw,nosuid,nodev,exec,size=128m" if harness.requires_executable_tmp() else "rw,nosuid,nodev,size=128m"},
            restart_policy={"Name": "unless-stopped"},
            ports={},
        )

    def recover_running_session(self, session: Session) -> bool:
        """Start one durable ``running`` session after an API/host restart.

        The database remains the lifecycle authority.  This method starts
        only the exact container ID it recorded, after proving the container
        still carries this session's AgentCC labels. It never recreates a
        missing container or resumes a user-suspended session.
        """
        if session.container_id is None:
            raise RuntimeUnavailable("running session has no persisted container ID")
        client = self._client()
        try:
            from docker.errors import NotFound

            try:
                container = client.containers.get(session.container_id)
            except NotFound as error:
                raise RuntimeUnavailable("persisted session container no longer exists") from error
            labels = container.labels or {}
            if labels.get("agentcc.managed") != "true" or labels.get("agentcc.kind") != "session" or labels.get("agentcc.session_id") != str(session.id):
                raise RuntimeUnavailable("refusing to recover an unverified session container")
            self._connect_control_plane(self._network(client))
            container.reload()
            if container.status == "running":
                return False
            if container.status == "paused":
                # A paused container represents a user-suspended lifecycle,
                # even if an older database record was not updated cleanly.
                return False
            container.start()
            return True
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable("unable to recover the persisted session container") from error

    def harness_tmux_exists(self, session: Session) -> bool:
        """Check the one durable harness tmux without creating it."""
        if session.container_id is None:
            return False
        client = self._client()
        try:
            container = client.containers.get(session.container_id)
            container.reload()
            return container.status == "running" and self._tmux_session_exists(client, container.id)
        except Exception:
            return False

    def ide_target(self, session: Session) -> str:
        if session.container_id is None:
            raise RuntimeUnavailable("session has no provisioned container")
        client = self._client()
        try:
            self._connect_control_plane(client.networks.get(self.network_name))
            container = client.containers.get(session.container_id)
            container.reload()
            if container.status != "running":
                raise RuntimeUnavailable("session container is not running")
            networks = container.attrs["NetworkSettings"]["Networks"]
            address = networks[self.network_name]["IPAddress"]
            if not address:
                raise RuntimeUnavailable("session container has no private IDE address")
            return f"http://{address}:8080"
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable("unable to reach the session IDE") from error

    def stop(self, container_id: str) -> None:
        client = self._client()
        try:
            client.containers.get(container_id).stop(timeout=10)
        except Exception as error:
            # A prior cleanup can remove a container while the durable session
            # metadata still exists.  Treat that as an already-stopped session
            # so the caller can reconcile its lifecycle state.
            try:
                from docker.errors import NotFound
                if isinstance(error, NotFound):
                    return
            except ImportError:
                pass
            raise RuntimeUnavailable("unable to stop the session container") from error

    def delete_session(self, session: Session) -> None:
        """Remove only the verified disposable runtime resources for a session."""
        client = self._client()
        try:
            from docker.errors import NotFound

            try:
                container = client.containers.get(session.container_id) if session.container_id else None
            except NotFound:
                container = None
            if container is not None:
                labels = container.labels or {}
                if labels.get("agentcc.managed") != "true" or labels.get("agentcc.kind") != "session" or labels.get("agentcc.session_id") != str(session.id):
                    raise RuntimeUnavailable("refusing to delete an unverified session container")
                container.remove(force=True, v=False)

            if session.volume_name:
                try:
                    volume = client.volumes.get(session.volume_name)
                except NotFound:
                    volume = None
                if volume is not None:
                    labels = volume.attrs.get("Labels") or {}
                    if labels.get("agentcc.managed") != "true" or labels.get("agentcc.kind") != "session-volume" or labels.get("agentcc.session_id") != str(session.id):
                        raise RuntimeUnavailable("refusing to delete an unverified session volume")
                    volume.remove(force=True)
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable("unable to delete the session runtime resources") from error

    def delete_workspace_storage(self, workspace: Workspace) -> None:
        """Permanently remove a verified durable workspace and its file assets."""
        client = self._client()
        try:
            from docker.errors import NotFound

            try:
                volume = client.volumes.get(workspace.volume_name)
            except NotFound:
                volume = None
            if volume is not None:
                labels = volume.attrs.get("Labels") or {}
                expected = self._workspace_volume_labels(workspace)
                if any(labels.get(key) != value for key, value in expected.items()):
                    raise RuntimeUnavailable("refusing to permanently delete an unverified workspace volume")
                # Never force this operation: Docker protects an attached
                # volume, preventing a hard delete from racing a session.
                volume.remove(force=False)
            if workspace.host_path:
                self._remove_workspace_directory(client, workspace)
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable("unable to permanently delete the durable workspace storage") from error

    def _remove_workspace_directory(self, client, workspace: Workspace) -> None:
        """Remove only the UUID-derived host child after a verified hard delete."""
        if not workspace.host_path:
            return
        root, child = os.path.dirname(workspace.host_path), os.path.basename(workspace.host_path)
        if root not in self.workspace_host_roots or child != workspace.folder_name:
            raise RuntimeUnavailable("refusing to remove an unverified workspace host path")
        client.containers.run(
            self.image,
            command=["-rf", f"/host/{child}"],
            entrypoint=["/usr/bin/rm"],
            user="0",
            volumes={root: {"bind": "/host", "mode": "rw"}},
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            cap_add=["DAC_OVERRIDE"],
            security_opt=["no-new-privileges:true"],
            remove=True,
            labels={"agentcc.managed": "true", "agentcc.kind": "workspace-remover"},
        )

    def suspend(self, container_id: str) -> None:
        client = self._client()
        try:
            client.containers.get(container_id).pause()
        except Exception as error:
            raise RuntimeUnavailable("unable to suspend the session container") from error

    def resume(self, container_id: str) -> None:
        client = self._client()
        try:
            client.containers.get(container_id).unpause()
        except Exception as error:
            raise RuntimeUnavailable("unable to resume the session container") from error

    def _install_harness_files(self, client, container_id: str, files: dict[str, str]) -> None:
        """Install fixed adapter files as the harness user, never browser paths."""
        if not files:
            return
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            created_dirs: set[str] = set()
            for absolute_path, content in files.items():
                if not absolute_path.startswith("/home/agent/"):
                    raise HarnessConfigurationError("adapter attempted to write outside the harness home")
                relative_path = absolute_path.removeprefix("/home/")
                directory = relative_path.rsplit("/", maxsplit=1)[0]
                if directory not in created_dirs:
                    entry = tarfile.TarInfo(directory)
                    entry.type = tarfile.DIRTYPE
                    entry.mode = 0o700
                    entry.uid = entry.gid = 10001
                    tar.addfile(entry)
                    created_dirs.add(directory)
                data = content.encode("utf-8")
                entry = tarfile.TarInfo(relative_path)
                entry.size = len(data)
                entry.mode = 0o700 if "/.agentcc/run-" in absolute_path else 0o600
                entry.uid = entry.gid = 10001
                tar.addfile(entry, io.BytesIO(data))
        if not client.api.put_archive(container_id, "/home", archive.getvalue()):
            raise RuntimeUnavailable("unable to install harness configuration")

    @staticmethod
    def _agent_command(*command: str) -> list[str]:
        return [
            "runuser", "--preserve-environment", "-u", "agent", "--", "env",
            "HOME=/home/agent", "TERM=xterm-256color", *command,
        ]

    def _exec_exit_code(self, client, container_id: str, command: list[str], environment: dict[str, str]) -> int:
        exec_id = client.api.exec_create(container_id, cmd=command, environment=environment)["Id"]
        client.api.exec_start(exec_id)
        return client.api.exec_inspect(exec_id).get("ExitCode", 1)

    def _exec_output(self, client, container_id: str, command: list[str], environment: dict[str, str]) -> tuple[int, bytes]:
        exec_id = client.api.exec_create(container_id, cmd=command, environment=environment)["Id"]
        output = client.api.exec_start(exec_id)
        return client.api.exec_inspect(exec_id).get("ExitCode", 1), output if isinstance(output, bytes) else bytes(output)

    def _read_journal_probe(self, client, container_id: str, probe: JournalTelemetryProbe, environment: dict[str, str]) -> bytes | None:
        code, listing = self._exec_output(
            client,
            container_id,
            self._agent_command("find", probe.directory, "-type", "f", "-name", probe.filename_pattern, "-printf", "%T@ %p\\n"),
            environment,
        )
        if code != 0 or not listing:
            return None
        newest = max(
            (line for line in listing.decode("utf-8", errors="ignore").splitlines() if f" {probe.directory}/" in line),
            default="",
        )
        path = newest.partition(" ")[2]
        if not path.startswith(f"{probe.directory}/"):
            return None
        code, payload = self._exec_output(
            client, container_id, self._agent_command("tail", "-c", str(probe.tail_bytes), path), environment
        )
        return payload if code == 0 else None

    def _read_sqlite_probe(self, client, container_id: str, probe: SqliteTelemetryProbe, environment: dict[str, str]) -> bytes | None:
        """Export only the active Hermes session's safe fields from state.db.

        A read-only SQLite connection is made inside the container, which is
        compatible with Hermes' WAL writer and avoids copying database files
        (and their sidecars) across the Docker API boundary.
        """
        program = "\n".join(
            [
                "import json, sqlite3, sys",
                "db = sys.argv[1]",
                "try:",
                "    conn = sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=1)",
                "    conn.row_factory = sqlite3.Row",
                "    tables = {row[0] for row in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")}",
                "    session = {}",
                "    messages = []",
                "    if 'sessions' in tables:",
                "        row = conn.execute('SELECT * FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()",
                "        session = dict(row) if row else {}",
                "    if session and 'messages' in tables and 'id' in session:",
                "        rows = conn.execute('SELECT * FROM messages WHERE session_id = ? ORDER BY rowid ASC', (session['id'],)).fetchall()",
                "        messages = [dict(row) for row in rows]",
                "    print(json.dumps({'session': session, 'messages': messages}, default=str))",
                "except Exception:",
                "    print('{}')",
            ]
        )
        code, payload = self._exec_output(
            client, container_id, self._agent_command("python3", "-c", program, probe.database_path), environment
        )
        return payload if code == 0 else None

    def _read_kilo_sqlite_probe(self, client, container_id: str, probe: KiloSqliteTelemetryProbe, environment: dict[str, str]) -> bytes | None:
        """Export Kilo's newest local session and safe text/step parts.

        Kilo stores structured sessions in SQLite. This query deliberately
        excludes tool inputs and outputs; only message role, text parts, and
        step-finish usage/cost data cross the Docker boundary.
        """
        program = "\n".join(
            [
                "import json, sqlite3, sys",
                "db = sys.argv[1]",
                "try:",
                "    conn = sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=1)",
                "    conn.row_factory = sqlite3.Row",
                "    session = conn.execute('SELECT id, time_updated, data FROM session ORDER BY time_updated DESC LIMIT 1').fetchone()",
                "    if not session: print('{}'); raise SystemExit",
                "    session_data = json.loads(session['data']) if session['data'] else {}",
                "    messages = {row['id']: json.loads(row['data']) if row['data'] else {} for row in conn.execute('SELECT id, data FROM message WHERE session_id = ?', (session['id'],))}",
                "    parts = []",
                "    for row in conn.execute('SELECT message_id, time_created, data FROM part WHERE session_id = ? ORDER BY time_created, id', (session['id'],)):",
                "        data = json.loads(row['data']) if row['data'] else {}",
                "        if data.get('type') in ('text', 'step-finish', 'error'): parts.append({'message': messages.get(row['message_id'], {}), 'time_created': row['time_created'], 'data': data})",
                "    print(json.dumps({'session': session_data, 'parts': parts}, default=str))",
                "except Exception:",
                "    print('{}')",
            ]
        )
        code, payload = self._exec_output(
            client, container_id, self._agent_command("python3", "-c", program, probe.database_path), environment
        )
        return payload if code == 0 else None

    def _read_probe_payload(self, client, container_id: str, probe, environment: dict[str, str]) -> bytes | None:
        if isinstance(probe, JournalTelemetryProbe):
            return self._read_journal_probe(client, container_id, probe, environment)
        if isinstance(probe, SqliteTelemetryProbe):
            return self._read_sqlite_probe(client, container_id, probe, environment)
        if isinstance(probe, KiloSqliteTelemetryProbe):
            return self._read_kilo_sqlite_probe(client, container_id, probe, environment)
        return None

    def _read_diagnostics(self, client, container_id: str, probe: SqliteTelemetryProbe, environment: dict[str, str]) -> bytes:
        if not probe.diagnostic_log_path:
            return b""
        code = self._exec_exit_code(client, container_id, self._agent_command("test", "-f", probe.diagnostic_log_path), environment)
        if code != 0:
            return b""
        code, payload = self._exec_output(
            client, container_id, self._agent_command("tail", "-c", str(probe.diagnostic_tail_bytes), probe.diagnostic_log_path), environment
        )
        return payload if code == 0 else b""

    def collect_usage(self, session: Session):
        """Collect normalized usage through the selected harness adapter."""
        if not session.container_id:
            return None
        try:
            adapter = harnesses.for_name(session.harness)
            probe = adapter.telemetry_probe()
            if probe is None:
                return None
            client = self._client()
            container = client.containers.get(session.container_id)
            container.reload()
            if container.status != "running":
                return None
            payload = self._read_probe_payload(client, container.id, probe, {"HOME": "/home/agent", "TERM": "xterm-256color"})
            return adapter.parse_telemetry(payload, session.tokens, session.model_calls) if payload else None
        except Exception:
            # Telemetry must never interfere with a live harness or leak a
            # provider/runtime exception into the browser.
            return None

    def collect_conversation(self, session: Session):
        if not session.container_id:
            return []
        try:
            adapter = harnesses.for_name(session.harness)
            probe = adapter.telemetry_probe()
            if probe is None:
                return []
            client = self._client()
            container = client.containers.get(session.container_id)
            container.reload()
            if container.status != "running":
                return []
            environment = {"HOME": "/home/agent", "TERM": "xterm-256color"}
            payload = self._read_probe_payload(client, container.id, probe, environment)
            diagnostics = self._read_diagnostics(client, container.id, probe, environment) if isinstance(probe, SqliteTelemetryProbe) else b""
            return adapter.parse_conversation(payload, diagnostics) if payload else []
        except Exception:
            return []

    def capture_terminal_output(self, session: Session, lines: int = 14) -> TerminalSnapshot | None:
        """Read the tail of the detached harness tmux without attaching to it.

        This is deliberately a screen snapshot rather than another PTY: polling
        the inspector must neither start a harness nor consume its terminal
        output. The bounded result is suitable for a compact status preview.
        """
        if session.container_id is None:
            raise RuntimeUnavailable("session has no provisioned container")
        client = self._client()
        try:
            container = client.containers.get(session.container_id)
            container.reload()
            if container.status != "running":
                raise RuntimeUnavailable("session container is not running")
            if not self._tmux_session_exists(client, container.id):
                return None
            code, command_output = self._exec_output(
                client,
                container.id,
                self._agent_command("tmux", "display-message", "-p", "-t", "agentcc:0.0", "#{pane_current_command}"),
                {"HOME": "/home/agent", "TERM": "xterm-256color"},
            )
            if code != 0:
                return None
            pane_command = command_output.decode("utf-8", errors="replace").strip()
            count = max(1, min(lines, 40))
            code, output = self._exec_output(
                client,
                container.id,
                self._agent_command("tmux", "capture-pane", "-p", "-J", "-t", "agentcc:0.0", "-S", f"-{count}"),
                {"HOME": "/home/agent", "TERM": "xterm-256color"},
            )
            if code != 0:
                return None
            text = output.decode("utf-8", errors="replace")[-4096:].rstrip()
            activity = harnesses.for_name(session.harness).classify_terminal_activity(text, pane_command)
            return TerminalSnapshot(text=text, agent_activity=activity)
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable("unable to read the persistent harness output") from error

    def agent_activity(self, session: Session) -> AgentActivity:
        """Return a dynamic sub-state without modifying the durable lifecycle."""
        if session.state == SessionState.SUSPENDED:
            return AgentActivity.PAUSED
        if session.state == SessionState.COMPLETED:
            return AgentActivity.STOPPED
        if session.state != SessionState.RUNNING:
            return AgentActivity.UNKNOWN
        try:
            snapshot = self.capture_terminal_output(session, lines=8)
            return snapshot.agent_activity if snapshot is not None else AgentActivity.NOT_STARTED
        except RuntimeUnavailable:
            return AgentActivity.UNKNOWN

    def _tmux_session_exists(self, client, container_id: str) -> bool:
        return self._exec_exit_code(
            client, container_id, self._agent_command("tmux", "has-session", "-t", "agentcc"),
            {"HOME": "/home/agent", "TERM": "xterm-256color"},
        ) == 0

    def _ensure_tmux_session(self, client, container_id: str, launch) -> bool:
        """Return whether this call created a replacement harness tmux."""
        if self._tmux_session_exists(client, container_id):
            return False
        if self._exec_exit_code(
            client, container_id, self._agent_command("tmux", "-V"), {"HOME": "/home/agent", "TERM": "xterm-256color"}
        ) != 0:
            raise RuntimeUnavailable(
                "this legacy session image does not include tmux, so it cannot start a replacement harness without replacing the container"
            )
        self._install_harness_files(client, container_id, launch.files)
        exit_code = self._exec_exit_code(
            client,
            container_id,
            self._agent_command("tmux", "new-session", "-d", "-s", "agentcc", launch.entrypoint),
            launch.environment,
        )
        # A concurrent browser attachment may have created the one allowed
        # session between has-session and new-session. It is safe to attach.
        if exit_code != 0 and not self._tmux_session_exists(client, container_id):
            raise RuntimeUnavailable("unable to start the persistent harness tmux session")
        created = exit_code == 0
        if created:
            # Keep terminal history available after browser reconnects. The
            # browser has its own scrollback too, but tmux is the durable
            # screen source when a new browser attachment is made.
            self._exec_exit_code(
                client,
                container_id,
                self._agent_command("tmux", "set-option", "-t", "agentcc", "history-limit", "10000"),
                {"HOME": "/home/agent", "TERM": "xterm-256color"},
            )
        if launch.initial_input:
            # Never send browser-controlled task text while the pane may still
            # be a shell: wait until the fixed harness binary has replaced the
            # launcher, then use tmux's literal-input mode (not shell source).
            for _ in range(150):
                code, current = self._exec_output(
                    client,
                    container_id,
                    self._agent_command("tmux", "display-message", "-p", "-t", "agentcc:0.0", "#{pane_current_command}"),
                    {"HOME": "/home/agent", "TERM": "xterm-256color"},
                )
                command_ready = code == 0 and current.decode("utf-8", errors="ignore").strip().lower() in launch.ready_commands
                if command_ready:
                    if not launch.ready_markers:
                        break
                    code, pane = self._exec_output(
                        client,
                        container_id,
                        self._agent_command("tmux", "capture-pane", "-p", "-J", "-t", "agentcc:0.0", "-S", "-120"),
                        {"HOME": "/home/agent", "TERM": "xterm-256color"},
                    )
                    visible = pane.decode("utf-8", errors="ignore") if code == 0 else ""
                    if all(marker in visible for marker in launch.ready_markers):
                        break
                time.sleep(0.1)
            else:
                raise RuntimeUnavailable("Hermes did not start its interactive terminal")
            sent = self._exec_exit_code(
                client,
                container_id,
                self._agent_command("tmux", "send-keys", "-t", "agentcc:0.0", "-l", "--", launch.initial_input),
                {"HOME": "/home/agent", "TERM": "xterm-256color"},
            )
            entered = self._exec_exit_code(
                client,
                container_id,
                self._agent_command("tmux", "send-keys", "-t", "agentcc:0.0", "Enter"),
                {"HOME": "/home/agent", "TERM": "xterm-256color"},
            )
            if sent != 0 or entered != 0:
                raise RuntimeUnavailable("unable to seed the Hermes task in its persistent terminal")
        return created

    def _configure_tmux_terminal(self, client, container_id: str) -> None:
        """Make tmux's durable pane history reachable from an xterm client."""
        environment = {"HOME": "/home/agent", "TERM": "xterm-256color"}
        # Mouse mode makes a browser wheel event enter/cycle tmux copy mode.
        # In that mode the terminal scrolls the detached pane's history rather
        # than xterm's transient alternate-screen buffer.
        self._exec_exit_code(
            client, container_id, self._agent_command("tmux", "set-option", "-t", "agentcc", "mouse", "on"), environment
        )
        self._exec_exit_code(
            client, container_id, self._agent_command("tmux", "set-option", "-t", "agentcc", "history-limit", "10000"), environment
        )

    def open_terminal(self, session: Session, model: RegisteredModel | None = None, api_key: str | None = None) -> TerminalPty:
        """Attach a browser PTY to the one persistent harness tmux session."""
        if session.container_id is None:
            raise RuntimeUnavailable("session has no provisioned container")
        client = self._client()
        try:
            container = client.containers.get(session.container_id)
            container.reload()
            if container.status != "running":
                raise RuntimeUnavailable("session container is not running")
            launch = harnesses.for_name(session.harness).prepare(session, model, api_key)
            created_harness = self._ensure_tmux_session(client, container.id, launch)
            self._configure_tmux_terminal(client, container.id)
            exec_id = client.api.exec_create(
                container.id,
                cmd=self._agent_command("tmux", "attach-session", "-t", "agentcc"),
                stdin=True,
                tty=True,
                environment={"HOME": "/home/agent", "TERM": "xterm-256color"},
                workdir=f"/workspaces/shared/{session.workspace_folder_name}",
            )["Id"]
            stream = client.api.exec_start(exec_id, socket=True, tty=True)
            return TerminalPty(stream=stream, client=client, exec_id=exec_id, created_harness=created_harness)
        except RuntimeUnavailable:
            raise
        except HarnessConfigurationError as error:
            raise RuntimeUnavailable(str(error)) from error
        except Exception as error:
            raise RuntimeUnavailable("unable to start the authorized harness terminal") from error


runtime = DockerContainerRuntime()
