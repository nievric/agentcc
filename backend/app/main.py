from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .harnesses import HarnessConfigurationError, harnesses
from .models import AgentActivity, AppEvent, ConversationMessage, DisplaySettings, DisplaySettingsUpdate, ModelRegistration, RegisteredModel, Session, SessionAction, SessionCreate, SessionLog, SessionLogTail, SessionOutput, SessionState, Summary, Telemetry, Workspace, WorkspaceCreate, WorkspaceDelete, WorkspaceStorageSettings, WorkspaceStorageSettingsUpdate, WorkspaceUsage, default_model_label
from .runtime import RuntimeUnavailable, runtime
from .store import store

app = FastAPI(title="Agent Command Center API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_HOP_BY_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def _ide_path(session_id: UUID, path: str) -> str:
    # code-server supports path-hosting when a reverse proxy strips the
    # external prefix before forwarding to its root route.
    return f"/{path}" if path else "/"


def _refresh_sessions() -> list[Session]:
    """Refresh the live fields needed by the fleet without serial Docker probes.

    Conversation capture has its own background collector. Keeping it off this
    request path means opening the fleet only reads the telemetry and activity
    necessary for its grid, and several running containers can be sampled at
    once without allowing an unbounded number of Docker exec calls.
    """
    sessions = store.list_sessions()
    running_sessions = [session for session in sessions if session.state == SessionState.RUNNING]

    def probe(session: Session):
        usage = None
        activity = session.agent_activity
        try:
            usage = runtime.collect_usage(session)
        except Exception:
            pass
        try:
            activity = runtime.agent_activity(session)
        except Exception:
            pass
        return session.id, usage, activity

    activities = {session.id: runtime.agent_activity(session) for session in sessions if session.state != SessionState.RUNNING}
    if running_sessions:
        workers = min(4, len(running_sessions))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fleet-probe") as executor:
            for session_id, usage, activity in executor.map(probe, running_sessions):
                if usage is not None:
                    store.update_usage(session_id, usage)
                activities[session_id] = activity

    # Usage collection persists new values, so reload before serializing the
    # response and then attach the intentionally non-persistent activity view.
    sessions = store.list_sessions()
    for session in sessions:
        session.agent_activity = activities.get(session.id, AgentActivity.UNKNOWN)
    return sessions


def _collect_session_transcripts() -> None:
    """Persist harness transcript events without requiring a browser PTY.

    ``save_conversation`` fingerprints every event, making the periodic scan
    idempotent while adapters remain free to use journal tails or another
    harness-native cursor in the future.
    """
    for session in store.list_sessions():
        if session.state != SessionState.RUNNING:
            continue
        try:
            store.save_conversation(session.id, runtime.collect_conversation(session))
        except RuntimeUnavailable:
            continue


def _recover_running_session_containers() -> None:
    """Reconcile durable running-session records after process/host restart."""
    if not runtime.enabled:
        return
    for session in store.list_sessions():
        if session.state != SessionState.RUNNING:
            continue
        try:
            started = runtime.recover_running_session(session)
        except RuntimeUnavailable as error:
            # Keep the durable lifecycle record for operator recovery rather
            # than silently turning a missing runtime into a completed task.
            store.append_log(session.id, "system", f"Startup recovery could not restore the session container: {error}\n")
        else:
            if started:
                store.append_log(session.id, "system", "Startup recovery restarted the session container. Its prior harness process ended; the next terminal attachment will create a new harness session.\n")
            elif not runtime.harness_tmux_exists(session):
                store.append_log(session.id, "system", "Startup recovery found no harness tmux session. The next terminal attachment will create a new harness session.\n")


async def _transcript_poll_loop() -> None:
    interval = max(2, int(os.getenv("AGENTCC_TRANSCRIPT_INTERVAL_SECONDS", "5")))
    while True:
        await asyncio.to_thread(_collect_session_transcripts)
        await asyncio.sleep(interval)


@app.on_event("startup")
async def start_transcript_collector() -> None:
    await asyncio.to_thread(_recover_running_session_containers)
    app.state.transcript_collector = asyncio.create_task(_transcript_poll_loop())


@app.on_event("shutdown")
async def stop_transcript_collector() -> None:
    task = getattr(app.state, "transcript_collector", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "agentcc-api"}


@app.get("/api/v1/system/summary", response_model=Summary)
def get_summary() -> Summary:
    return store.summary()


@app.get("/api/v1/telemetry", response_model=Telemetry)
def get_telemetry() -> Telemetry:
    return store.telemetry()


@app.get("/api/v1/sessions", response_model=list[Session])
def list_sessions() -> list[Session]:
    return _refresh_sessions()


@app.post("/api/v1/sessions", response_model=Session, status_code=201)
def create_session(payload: SessionCreate) -> Session:
    workspace = store.get_workspace(payload.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not runtime.enabled:
        raise HTTPException(status_code=503, detail="container runtime is disabled; start with deploy/compose.runtime.yaml")
    selected_model: RegisteredModel | None = None
    if payload.model_id is not None:
        selected_model = store.get_model(payload.model_id)
        if selected_model is None or not selected_model.enabled:
            raise HTTPException(status_code=404, detail="registered model not found")
    try:
        harnesses.for_name(payload.harness).validate_model(selected_model)
    except HarnessConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    session_data = payload.model_dump()
    if selected_model is not None:
        session_data["model"] = selected_model.model_name
    else:
        # The stored label is for operators; each adapter owns the actual
        # native default selection behavior.
        session_data["model"] = default_model_label(payload.harness)
    session = Session(**session_data, workspace=workspace.name, workspace_folder_name=workspace.folder_name)
    try:
        provisioned = runtime.provision(session, workspace, selected_model)
    except RuntimeUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    session.container_id = provisioned.container_id
    session.volume_name = provisioned.volume_name
    return store.add_session(session)


@app.get("/api/v1/models", response_model=list[RegisteredModel])
def list_models() -> list[RegisteredModel]:
    return store.list_models()


@app.post("/api/v1/models", response_model=RegisteredModel, status_code=201)
def register_model(payload: ModelRegistration) -> RegisteredModel:
    try:
        return store.register_model(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.delete("/api/v1/models/{model_id}", status_code=204)
def delete_model(model_id: UUID) -> Response:
    try:
        deleted = store.delete_model(model_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="registered model not found")
    return Response(status_code=204)


@app.post("/api/v1/sessions/{session_id}/actions", response_model=Session)
def session_action(session_id: UUID, payload: SessionAction) -> Session:
    existing = store.get_session(session_id)
    if existing is not None and existing.container_id is not None:
        try:
            if payload.action == "stop":
                store.save_conversation(session_id, runtime.collect_conversation(existing))
                runtime.stop(existing.container_id)
            elif payload.action == "suspend":
                runtime.suspend(existing.container_id)
            elif payload.action == "resume":
                runtime.resume(existing.container_id)
        except RuntimeUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    session = store.act_on_session(session_id, payload)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.delete("/api/v1/sessions/{session_id}", status_code=204)
def delete_session(session_id: UUID) -> Response:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.state != SessionState.COMPLETED:
        raise HTTPException(status_code=409, detail="stop the session before deleting it")
    if session.container_id is not None or session.volume_name is not None:
        try:
            runtime.delete_session(session)
        except RuntimeUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    if not store.delete_session(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return Response(status_code=204)


@app.get("/api/v1/sessions/{session_id}/logs", response_model=list[SessionLog])
def list_session_logs(session_id: UUID) -> list[SessionLog]:
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return store.list_logs(session_id)


@app.get("/api/v1/sessions/{session_id}/conversation", response_model=list[ConversationMessage])
def session_conversation(session_id: UUID) -> list[ConversationMessage]:
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return store.conversation(session_id)


@app.get("/api/v1/sessions/{session_id}/logs/tail", response_model=SessionLogTail)
def session_log_tail(session_id: UUID) -> SessionLogTail:
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return store.session_log_tail(session_id)


@app.get("/api/v1/sessions/{session_id}/logs/download")
def download_session_logs(session_id: UUID) -> FileResponse:
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    log_path = store.session_log_file(session_id)
    filename = f"agentcc-session-{session_id}.jsonl"
    if log_path is not None:
        return FileResponse(log_path, media_type="application/x-ndjson", filename=filename)
    entries = store.list_logs(session_id)
    if not entries:
        raise HTTPException(status_code=404, detail="session log file not found")
    payload = "".join(json.dumps({"occurred_at": entry.occurred_at.isoformat(), "stream": entry.stream, "text": entry.text}, separators=(",", ":")) + "\n" for entry in entries)
    return Response(content=payload, media_type="application/x-ndjson", headers={"content-disposition": f'attachment; filename="{filename}"'})


@app.get("/api/v1/sessions/{session_id}/output", response_model=SessionOutput)
def session_output(session_id: UUID) -> SessionOutput:
    """Return the visible tail of the session-owned, detached tmux terminal."""
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        snapshot = runtime.capture_terminal_output(session)
    except RuntimeUnavailable as error:
        return SessionOutput(message=str(error))
    if snapshot is None:
        return SessionOutput(message="Awaiting harness output. Open the interactive terminal to start this session.", agent_activity=runtime.agent_activity(session))
    return SessionOutput(text=snapshot.text, available=True, agent_activity=snapshot.agent_activity)


@app.api_route("/api/v1/sessions/{session_id}/ide/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def ide_http_proxy(session_id: UUID, path: str, request: Request) -> Response:
    """Same-origin HTTP relay to one unexposed code-server instance."""
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        target = runtime.ide_target(session)
    except RuntimeUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    query = f"?{request.url.query}" if request.url.query else ""
    headers = {key: value for key, value in request.headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS | {"host"}}
    headers["host"] = request.headers.get("host", "localhost")
    headers["x-forwarded-host"] = request.headers.get("host", "localhost")
    headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", request.url.scheme)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
            upstream = await client.request(request.method, f"{target}{_ide_path(session_id, path)}{query}", headers=headers, content=await request.body())
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="session IDE did not respond") from error
    response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS | {"content-length"}}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))


@app.websocket("/api/v1/sessions/{session_id}/ide/{path:path}")
async def ide_websocket_proxy(websocket: WebSocket, session_id: UUID, path: str) -> None:
    """Relay IDE WebSockets without exposing a session container port."""
    session = store.get_session(session_id)
    if session is None:
        await websocket.accept()
        await websocket.close(code=4404)
        return
    try:
        target = runtime.ide_target(session).replace("http://", "ws://", 1)
    except RuntimeUnavailable:
        await websocket.accept()
        await websocket.close(code=1011)
        return
    query = f"?{websocket.url.query}" if websocket.url.query else ""
    try:
        async with websockets.connect(f"{target}{_ide_path(session_id, path)}{query}", max_size=None) as upstream:
            await websocket.accept()

            async def to_ide() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def from_ide() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            done, pending = await asyncio.wait({asyncio.create_task(to_ide()), asyncio.create_task(from_ide())}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


@app.websocket("/api/v1/sessions/{session_id}/terminal")
async def session_terminal(websocket: WebSocket, session_id: UUID) -> None:
    """Attach a browser client to the session-owned persistent harness tmux."""
    await websocket.accept()
    session = store.get_session(session_id)
    if session is None:
        await websocket.send_json({"type": "error", "message": "Session not found."})
        await websocket.close(code=4404)
        return
    try:
        first_harness_start = not session.harness_started
        model = None
        api_key = None
        if session.model_id is not None:
            model = store.get_model(session.model_id)
            if model is None:
                raise RuntimeUnavailable("registered model is unavailable")
            api_key = store.model_secret(session.model_id)
        pty = runtime.open_terminal(session, model=model, api_key=api_key)
    except RuntimeUnavailable as error:
        message = str(error)
        store.append_log(session_id, "system", f"Terminal unavailable: {message}\n")
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=1011)
        return

    except Exception:
        # Credential failures must be visible to the operator without exposing
        # an endpoint, encrypted value, or provider secret in the browser.
        message = "Unable to prepare the selected model credential. Check the model registry vault configuration."
        store.append_log(session_id, "system", f"Terminal unavailable: {message}\n")
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=1011)
        return

    if pty.created_harness:
        store.mark_harness_started(session_id)
        notice = (
            f"Started the persistent {session.harness} harness session."
            if first_harness_start
            else "The previous harness terminal was unavailable, so AgentCC started a new harness session. Use your harness’s resume flow if you need to continue its prior conversation."
        )
        store.append_log(session_id, "system", f"{notice}\n")
        await websocket.send_json({"type": "notice", "message": notice})
    else:
        store.append_log(session_id, "system", f"Attached browser to persistent {session.harness} tmux session.\n")

    async def relay_output() -> None:
        try:
            while True:
                chunk = await asyncio.to_thread(pty.read)
                if not chunk:
                    return
                store.append_log(session_id, "stdout", chunk.decode("utf-8", errors="replace"))
                await websocket.send_bytes(chunk)
        except Exception:
            store.append_log(session_id, "system", "Terminal attachment relay ended unexpectedly.\n")

    async def relay_input() -> None:
        while True:
            payload = json.loads(await websocket.receive_text())
            message_type = payload.get("type")
            if message_type == "input" and isinstance(payload.get("data"), str):
                await asyncio.to_thread(pty.write, payload["data"].encode("utf-8"))
            elif message_type == "resize":
                columns, rows = payload.get("cols"), payload.get("rows")
                if isinstance(columns, int) and isinstance(rows, int) and columns > 0 and rows > 0:
                    await asyncio.to_thread(pty.resize, columns, rows)

    output_task = asyncio.create_task(relay_output())
    input_task = asyncio.create_task(relay_input())
    try:
        done, pending = await asyncio.wait({output_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                task.result()
            except (WebSocketDisconnect, json.JSONDecodeError):
                pass
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        for task in (output_task, input_task):
            task.cancel()
        # Closing this Docker exec closes only the browser's tmux client. The
        # detached tmux server continues to own the harness process.
        pty.close()


@app.get("/api/v1/workspace-sessions", response_model=list[Workspace])
def list_workspace_sessions() -> list[Workspace]:
    return store.list_workspaces()


@app.get("/api/v1/workspaces/deleted", response_model=list[Workspace])
def list_deleted_workspaces() -> list[Workspace]:
    return store.list_workspaces(deleted=True)


@app.get("/api/v1/workspaces/usage", response_model=list[WorkspaceUsage])
def list_workspace_usage() -> list[WorkspaceUsage]:
    """Lazy workspace telemetry; deliberately separate from fleet refreshes."""
    results: list[WorkspaceUsage] = []
    for workspace in [*store.list_workspaces(), *store.list_workspaces(deleted=True)]:
        usage = runtime.workspace_usage(workspace)
        if usage is None:
            results.append(WorkspaceUsage(workspace_id=workspace.id))
        else:
            file_count, storage_bytes = usage
            results.append(WorkspaceUsage(workspace_id=workspace.id, file_count=file_count, storage_bytes=storage_bytes, available=True))
    return results


@app.get("/api/v1/settings/workspace-storage", response_model=WorkspaceStorageSettings)
def get_workspace_storage_settings() -> WorkspaceStorageSettings:
    configured = store.workspace_host_root()
    selected = runtime.default_workspace_host_root if configured is None else configured or None
    return WorkspaceStorageSettings(workspace_host_root=selected, available_workspace_host_roots=list(runtime.workspace_host_roots))


@app.put("/api/v1/settings/workspace-storage", response_model=WorkspaceStorageSettings)
def update_workspace_storage_settings(payload: WorkspaceStorageSettingsUpdate) -> WorkspaceStorageSettings:
    selected = payload.workspace_host_root
    if selected is not None and selected not in runtime.workspace_host_roots:
        raise HTTPException(status_code=422, detail="workspace host root is not an approved local runtime location")
    store.set_workspace_host_root(selected)
    return WorkspaceStorageSettings(workspace_host_root=selected, available_workspace_host_roots=list(runtime.workspace_host_roots))


@app.get("/api/v1/settings/display", response_model=DisplaySettings)
def get_display_settings() -> DisplaySettings:
    return DisplaySettings(timezone=store.display_timezone())


@app.put("/api/v1/settings/display", response_model=DisplaySettings)
def update_display_settings(payload: DisplaySettingsUpdate) -> DisplaySettings:
    timezone_name = payload.timezone.strip() if payload.timezone else None
    if timezone_name:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise HTTPException(status_code=422, detail="timezone must be a valid IANA name, such as America/Los_Angeles") from error
    store.set_display_timezone(timezone_name)
    return DisplaySettings(timezone=timezone_name)


@app.post("/api/v1/workspaces", response_model=Workspace, status_code=201)
def create_workspace(payload: WorkspaceCreate) -> Workspace:
    # A UUID-derived name is deterministic and never includes browser input.
    from uuid import uuid4

    workspace_id = uuid4()
    volume_name = f"agentcc-workspace-{str(workspace_id).split('-', maxsplit=1)[0]}"
    base_name = re.sub(r"[^a-z0-9]+", "-", payload.name.strip().lower()).strip("-") or "workspace"
    folder_name = f"{base_name[:80].rstrip('-')}-{str(workspace_id)[:8]}"
    configured_root = store.workspace_host_root()
    selected_root = runtime.default_workspace_host_root if configured_root is None else configured_root or None
    try:
        host_path = runtime.workspace_host_path(folder_name, selected_root)
    except RuntimeUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    candidate = Workspace(id=workspace_id, name=payload.name.strip(), description=payload.description.strip(), status="ready" if runtime.enabled else "runtime required", attached_sessions=0, last_opened="Not opened yet", volume_name=volume_name, folder_name=folder_name, host_path=host_path)
    if runtime.enabled:
        try:
            runtime.create_workspace_volume(candidate)
        except RuntimeUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    try:
        return store.create_workspace(payload, workspace_id=workspace_id, volume_name=volume_name, folder_name=folder_name, status=candidate.status, host_path=host_path)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/api/v1/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: UUID, payload: WorkspaceDelete) -> Response:
    workspace = store.get_workspace(workspace_id, include_deleted=True)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    try:
        if payload.mode == "soft":
            if workspace.deleted_at is not None:
                raise HTTPException(status_code=409, detail="workspace is already in trash")
            if store.soft_delete_workspace(workspace_id) is None:
                raise HTTPException(status_code=409, detail="workspace could not be moved to trash")
            return Response(status_code=204)
        if store.workspace_has_sessions(workspace_id):
            raise HTTPException(status_code=409, detail="delete all agent sessions associated with this workspace before permanently deleting it")
        if runtime.enabled:
            runtime.delete_workspace_storage(workspace)
        elif workspace.status != "runtime required":
            raise HTTPException(status_code=503, detail="container runtime is required to permanently delete durable workspace storage")
        if not store.hard_delete_workspace(workspace_id):
            raise HTTPException(status_code=404, detail="workspace not found")
        return Response(status_code=204)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/v1/workspaces/{workspace_id}/restore", response_model=Workspace)
def restore_workspace(workspace_id: UUID) -> Workspace:
    workspace = store.get_workspace(workspace_id, include_deleted=True)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if workspace.deleted_at is None:
        raise HTTPException(status_code=409, detail="workspace is not in trash")
    restored = store.restore_workspace(workspace_id, "ready" if runtime.enabled else "runtime required")
    if restored is None:
        raise HTTPException(status_code=409, detail="workspace could not be restored")
    return restored


@app.get("/api/v1/events", response_model=list[AppEvent])
def list_events() -> list[AppEvent]:
    return store.events()
