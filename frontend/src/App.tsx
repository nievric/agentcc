import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import "@xterm/xterm/css/xterm.css";
import { AgentActivity, api, ConversationMessage, DisplaySettings, RegisteredModel, Session, SessionOutput, Summary, Telemetry, Workspace, WorkspaceStorageSettings, WorkspaceUsage } from "./api";

type View = "fleet" | "workspaces" | "terminal" | "logs" | "models" | "settings";
type AppData = { sessions: Session[]; summary: Summary; telemetry: Telemetry; workspaces: Workspace[]; deletedWorkspaces: Workspace[]; models: RegisteredModel[]; workspaceStorage: WorkspaceStorageSettings; displaySettings: DisplaySettings };
type ConfirmationRequest = { title: string; message: string; confirmLabel: string; onConfirm: () => Promise<void> | void };

const emptyData: AppData = {
  sessions: [],
  summary: { total_sessions: 0, running: 0, suspended: 0, idle: 0, tokens_last_hour: 0, tokens_last_24_hours: 0, tokens_last_7_days: 0, estimated_cost_usd: 0, estimated_cost_available: false, uptime_label: "Connecting…" },
  telemetry: { cpu_percent: 0, memory_used_gb: 0, memory_total_gb: 32, active_sessions: 0, session_tokens: 0, latency_ms: 0 },
  workspaces: [], deletedWorkspaces: [], models: [], workspaceStorage: { workspace_host_root: null, available_workspace_host_roots: [] }, displaySettings: { timezone: null }
};

const navItems: { id: View; icon: string; label: string }[] = [
  { id: "workspaces", icon: "▣", label: "Workspaces" },
  { id: "fleet", icon: "◈", label: "Agent Fleet" },
  { id: "terminal", icon: "›_", label: "Interactive Terminal" },
  { id: "logs", icon: "≡", label: "Session Logs" },
  { id: "models", icon: "⌘", label: "Models & Keys" },
  { id: "settings", icon: "⚙", label: "Settings & Config" }
];

function stateLabel(state: Session["state"]) {
  return state === "running" ? "Running" : state === "suspended" ? "Suspended" : state === "idle" ? "Idle" : "Completed";
}

function activityLabel(activity: AgentActivity) {
  return activity === "working" ? "Working" : activity === "waiting_for_input" ? "Waiting for input" : activity === "not_started" ? "Awaiting terminal" : activity === "paused" ? "Paused" : activity === "stopped" ? "Stopped" : "Active";
}

function formatTokens(tokens: number) {
  return tokens > 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
}

function formatCost(session: Session) {
  return session.cost_estimate_available ? `$${session.cost_usd.toFixed(2)}` : "—";
}

function formatRate(rate?: number | null) {
  return rate == null ? "—" : `$${rate.toLocaleString(undefined, { maximumFractionDigits: 4 })}/M`;
}

function formatStorage(bytes?: number | null) {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024; let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: value < 10 ? 1 : 0 })} ${units[index]}`;
}

function formatDuration(session: Session) {
  const started = new Date(session.started_at).getTime();
  const ended = session.ended_at ? new Date(session.ended_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.floor((ended - started) / 1000));
  const hours = Math.floor(seconds / 3600); const minutes = Math.floor((seconds % 3600) / 60); const remainder = seconds % 60;
  return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function formatDisplayTime(value: string, timezone?: string | null, includeDate = true) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown time";
  return includeDate
    ? date.toLocaleString(undefined, { timeZone: timezone || undefined, timeZoneName: "short" })
    : date.toLocaleTimeString(undefined, { timeZone: timezone || undefined, timeZoneName: "short" });
}

export default function App() {
  const [view, setView] = useState<View>("fleet");
  const [data, setData] = useState<AppData>(emptyData);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [terminalSessionId, setTerminalSessionId] = useState<string | null>(null);
  const [logSessionId, setLogSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showLaunch, setShowLaunch] = useState(false);
  const [launchWorkspaceId, setLaunchWorkspaceId] = useState<string | null>(null);
  const [showWorkspace, setShowWorkspace] = useState(false);
  const [confirmation, setConfirmation] = useState<ConfirmationRequest | null>(null);

  const refreshSupportingData = async () => {
    try {
      const [workspaces, deletedWorkspaces, models, workspaceStorage, displaySettings] = await Promise.all([api.workspaces(), api.deletedWorkspaces(), api.models(), api.workspaceStorageSettings(), api.displaySettings()]);
      setData((current) => ({ ...current, workspaces, deletedWorkspaces, models, workspaceStorage, displaySettings }));
    } catch {
      // Supporting data is loaded on demand and must not delay the fleet.
    }
  };

  const refresh = async () => {
    try {
      const [sessions, summary, telemetry] = await Promise.all([api.sessions(), api.summary(), api.telemetry()]);
      setData((current) => ({ ...current, sessions, summary, telemetry }));
      setSelectedId((current) => current ?? sessions[0]?.id ?? null);
      setError(null);
    } catch {
      setError("Unable to reach the AgentCC API. Start the backend and retry.");
    } finally {
      setLoading(false);
    }
    void refreshSupportingData();
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(timer);
  }, []);

  const selected = useMemo(() => data.sessions.find((session) => session.id === selectedId) ?? data.sessions[0], [data.sessions, selectedId]);

  const act = async (action: "suspend" | "resume" | "stop") => {
    if (!selected) return;
    try {
      await api.actOnSession(selected.id, action);
      await refresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Could not update session state");
    }
  };

  const deleteSession = () => {
    if (!selected) return;
    const session = selected;
    setConfirmation({
      title: "Delete agent session?",
      message: `${session.name}'s container, private session files, and retained logs will be removed. The durable workspace ${session.workspace} will be kept.`,
      confirmLabel: "Delete session",
      onConfirm: async () => {
        try {
          await api.deleteSession(session.id);
          setSelectedId(null);
          setTerminalSessionId((current) => current === session.id ? null : current);
          await refresh();
        } catch (error) {
          setError(error instanceof Error ? error.message : "Could not delete session");
        }
      }
    });
  };

  const onCreated = async () => {
    setShowLaunch(false);
    setLaunchWorkspaceId(null);
    await refresh();
  };

  const openLaunch = (workspaceId?: string) => {
    setLaunchWorkspaceId(workspaceId ?? null);
    setShowLaunch(true);
  };

  const onWorkspaceCreated = async () => {
    setShowWorkspace(false);
    await refresh();
  };

  const deleteWorkspace = async (workspaceId: string, mode: "soft" | "hard") => {
    await api.deleteWorkspace(workspaceId, mode);
    await refresh();
    await refreshSupportingData();
  };

  const restoreWorkspace = async (workspaceId: string) => {
    await api.restoreWorkspace(workspaceId);
    await refresh();
    await refreshSupportingData();
  };

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><img className="brand-mark" src="/agentcc-icon.png" alt="" /><span>AgentCC</span></div>
        <button className="launch-button" onClick={() => openLaunch()}><span>＋</span> Launch Agent</button>
        <p className="rail-label">Operational units</p>
        <nav>{navItems.slice(0, 4).map((item) => <NavItem key={item.id} item={item} active={view === item.id} badge={item.id === "fleet" ? `${data.summary.running}/${data.summary.total_sessions}` : undefined} onClick={() => setView(item.id)} />)}</nav>
        <p className="rail-label">Infrastructure</p>
        <nav>{navItems.slice(4).map((item) => <NavItem key={item.id} item={item} active={view === item.id} onClick={() => setView(item.id)} />)}</nav>
        <div className="sidebar-metrics">
          <p className="rail-label">Telemetry</p>
          <Metric label="CPU cluster" value={`${data.telemetry.cpu_percent}%`} />
          <Metric label="Memory" value={`${data.telemetry.memory_used_gb} / ${data.telemetry.memory_total_gb} GB`} />
          <Metric label="Session tokens" value={formatTokens(data.telemetry.session_tokens)} />
        </div>
      </aside>

      <section className="center-pane">
        <header className="topbar">
          <div><span className="crumb">Agent Command Center</span><span className="slash">/</span><strong>{view === "fleet" ? "Fleet Orchestration" : navItems.find((item) => item.id === view)?.label}</strong></div>
          <div className="topbar-right"><span className="latency">● {data.telemetry.latency_ms}ms</span><button className="icon-button" aria-label="Refresh" onClick={() => void refresh()}>↻</button></div>
        </header>
        {error && <div className="error-banner">{error}</div>}
        {loading ? <div className="loading">Synchronizing operational grid…</div> : <Content view={view} data={data} selected={selected} terminalSession={data.sessions.find((session) => session.id === terminalSessionId) ?? selected} logSession={data.sessions.find((session) => session.id === logSessionId) ?? selected} onSelect={setSelectedId} onSelectTerminal={(sessionId) => { setSelectedId(sessionId); setTerminalSessionId(sessionId); }} onSelectLog={(sessionId) => { setSelectedId(sessionId); setLogSessionId(sessionId); }} onOpenTerminal={(sessionId) => { setSelectedId(sessionId); setTerminalSessionId(sessionId); setView("terminal"); }} onOpenLogs={(sessionId) => { setSelectedId(sessionId); setLogSessionId(sessionId); setView("logs"); }} onOpenIde={(sessionId) => { setSelectedId(sessionId); window.open(`/api/v1/sessions/${sessionId}/ide/`, "_blank", "noopener"); }} onLaunch={openLaunch} onCreateWorkspace={() => setShowWorkspace(true)} onDeleteWorkspace={deleteWorkspace} onRestoreWorkspace={restoreWorkspace} onRefresh={refresh} />}
      </section>

      <aside className="inspector">
        <Inspector session={selected} onAction={act} onDelete={deleteSession} />
      </aside>

      {showLaunch && <LaunchDialog workspaces={data.workspaces} models={data.models} initialWorkspaceId={launchWorkspaceId} onClose={() => { setShowLaunch(false); setLaunchWorkspaceId(null); }} onCreated={onCreated} />}
      {showWorkspace && <WorkspaceDialog onClose={() => setShowWorkspace(false)} onCreated={onWorkspaceCreated} />}
      {confirmation && <ConfirmDialog request={confirmation} onClose={() => setConfirmation(null)} />}
    </main>
  );
}

function NavItem({ item, active, badge, onClick }: { item: { id: View; icon: string; label: string }; active: boolean; badge?: string; onClick: () => void }) {
  return <button className={`nav-item ${active ? "active" : ""}`} onClick={onClick}><span>{item.icon}</span>{item.label}{badge && <b title="Running sessions / total sessions">{badge}</b>}</button>;
}

function ConfirmDialog({ request, onClose }: { request: ConfirmationRequest; onClose: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const confirm = async () => {
    setConfirming(true);
    try {
      await request.onConfirm();
      onClose();
    } finally {
      setConfirming(false);
    }
  };
  return <div className="modal-backdrop confirmation-backdrop" role="presentation"><section className="confirmation-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirmation-title"><div className="confirmation-icon">!</div><div><p className="eyebrow">Confirmation required</p><h2 id="confirmation-title">{request.title}</h2><p>{request.message}</p></div><div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose} disabled={confirming}>Cancel</button><button type="button" className="danger-button confirmation-action" onClick={() => void confirm()} disabled={confirming}>{confirming ? "Working…" : request.confirmLabel}</button></div></section></div>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="sidebar-metric"><span>{label}</span><strong>{value}</strong></div>;
}

function Content({ view, data, selected, terminalSession, logSession, onSelect, onSelectTerminal, onSelectLog, onOpenTerminal, onOpenLogs, onOpenIde, onLaunch, onCreateWorkspace, onDeleteWorkspace, onRestoreWorkspace, onRefresh }: { view: View; data: AppData; selected?: Session; terminalSession?: Session; logSession?: Session; onSelect: (id: string) => void; onSelectTerminal: (id: string) => void; onSelectLog: (id: string) => void; onOpenTerminal: (id: string) => void; onOpenLogs: (id: string) => void; onOpenIde: (id: string) => void; onLaunch: (workspaceId?: string) => void; onCreateWorkspace: () => void; onDeleteWorkspace: (id: string, mode: "soft" | "hard") => Promise<void>; onRestoreWorkspace: (id: string) => Promise<void>; onRefresh: () => Promise<void> }) {
  if (view === "workspaces") return <WorkspaceView workspaces={data.workspaces} deletedWorkspaces={data.deletedWorkspaces} sessions={data.sessions} onLaunch={onLaunch} onCreateWorkspace={onCreateWorkspace} onOpenIde={onOpenIde} onDelete={onDeleteWorkspace} onRestore={onRestoreWorkspace} />;
  if (view === "terminal") return <TerminalView sessions={data.sessions} session={terminalSession} onSelect={onSelectTerminal} />;
  if (view === "logs") return <SessionLogsView sessions={data.sessions} session={logSession} timezone={data.displaySettings.timezone} onSelect={onSelectLog} />;
  if (view === "models") return <ModelsView models={data.models} onCreated={onRefresh} />;
  if (view === "settings") return <SettingsView workspaceStorage={data.workspaceStorage} displaySettings={data.displaySettings} onSaved={onRefresh} />;
  if (view !== "fleet") return <PlaceholderView view={view} />;
  return <FleetView data={data} selected={selected} onSelect={onSelect} onOpenTerminal={onOpenTerminal} onOpenLogs={onOpenLogs} onOpenIde={onOpenIde} onLaunch={onLaunch} />;
}

function FleetView({ data, selected, onSelect, onOpenTerminal, onOpenLogs, onOpenIde, onLaunch }: { data: AppData; selected?: Session; onSelect: (id: string) => void; onOpenTerminal: (id: string) => void; onOpenLogs: (id: string) => void; onOpenIde: (id: string) => void; onLaunch: (workspaceId?: string) => void }) {
  const cards = [
    ["Total sessions", String(data.summary.total_sessions), "◎", "neutral"],
    ["Active fleet", `${data.summary.running} Running`, "●", "green"],
    ["Estimated cost", data.summary.estimated_cost_available ? `$${data.summary.estimated_cost_usd.toFixed(2)}` : "Not configured", "$", "purple"]
  ];
  return <div className="page">
    <div className="page-heading"><div><p className="eyebrow">Fleet orchestration</p><h1>Live operational grid</h1><p>Sessions are isolated containers when the local runtime is enabled; workspace volumes persist after they stop.</p></div><button className="primary-button" onClick={() => onLaunch()}>＋ Launch Agent Harness</button></div>
    <div className="stat-grid">{cards.slice(0, 2).map(([label, value, icon, color]) => <article className="stat-card" key={label}><span className={`stat-icon ${color}`}>{icon}</span><p>{label}</p><strong>{value}</strong><small>{label === "Active fleet" ? `${data.summary.suspended} suspended · ${data.summary.idle} idle` : "Recorded sessions"}</small></article>)}<article className="stat-card token-usage-card"><span className="stat-icon blue">ϟ</span><p>Rolling token usage</p><div className="token-windows"><span><small>1h</small><strong>{formatTokens(data.summary.tokens_last_hour)}</strong></span><span><small>24h</small><strong>{formatTokens(data.summary.tokens_last_24_hours)}</strong></span><span><small>7d</small><strong>{formatTokens(data.summary.tokens_last_7_days)}</strong></span></div><small>Observed model usage</small></article>{cards.slice(2).map(([label, value, icon, color]) => <article className="stat-card" key={label}><span className={`stat-icon ${color}`}>{icon}</span><p>{label}</p><strong>{value}</strong><small>Configured model pricing</small></article>)}</div>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Active sessions</p><h2>Harness & workspace status</h2></div><span className="filter-chip">All workspaces ({data.workspaces.length})</span></div><div className="session-table"><div className="table-head"><span>Agent session & workspace</span><span>Harness & model</span><span>Status & telemetry</span><span>Activity</span></div>{data.sessions.map((session) => <div className={`session-row ${selected?.id === session.id ? "selected" : ""}`} key={session.id} role="button" tabIndex={0} onClick={() => onSelect(session.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onSelect(session.id); }}><span><strong>{session.name}</strong><small>{session.workspace}</small></span><span><strong>{session.harness}</strong><small>{session.model}</small></span><span><Status state={session.state} />{session.state === "running" && <ActivityStatus activity={session.agent_activity} />}<small>{formatTokens(session.tokens)} tokens · {formatCost(session)} estimated</small></span><span><strong>{session.model_calls} model calls</strong><small>{session.input_tokens.toLocaleString()} in · {session.output_tokens.toLocaleString()} out</small><span className="row-actions"><button className="terminal-link" onClick={(event) => { event.stopPropagation(); onOpenLogs(session.id); }}>View logs →</button>{session.state === "running" && <><button className="terminal-link" onClick={(event) => { event.stopPropagation(); onOpenTerminal(session.id); }}>Open terminal →</button><button className="terminal-link" onClick={(event) => { event.stopPropagation(); onOpenIde(session.id); }}>Open VS Code ↗</button></>}</span></span></div>)}</div></section>
  </div>;
}

function WorkspaceView({ workspaces, deletedWorkspaces, sessions, onLaunch, onCreateWorkspace, onOpenIde, onDelete, onRestore }: { workspaces: Workspace[]; deletedWorkspaces: Workspace[]; sessions: Session[]; onLaunch: (workspaceId?: string) => void; onCreateWorkspace: () => void; onOpenIde: (sessionId: string) => void; onDelete: (id: string, mode: "soft" | "hard") => Promise<void>; onRestore: (id: string) => Promise<void> }) {
  const [pendingDelete, setPendingDelete] = useState<Workspace | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [usage, setUsage] = useState<Record<string, WorkspaceUsage>>({});
  const [usageLoading, setUsageLoading] = useState(false);
  const refreshUsage = async () => {
    setUsageLoading(true); setMessage(null);
    try {
      const records = await api.workspaceUsage();
      setUsage(Object.fromEntries(records.map((record) => [record.workspace_id, record])));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not refresh workspace usage");
    } finally { setUsageLoading(false); }
  };
  useEffect(() => { void refreshUsage(); }, []);
  const remove = async (workspace: Workspace, mode: "soft" | "hard") => {
    try { setMessage(null); await onDelete(workspace.id, mode); setPendingDelete(null); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not delete workspace"); throw error; }
  };
  const restore = async (workspace: Workspace) => {
    setRestoringId(workspace.id); setMessage(null);
    try { await onRestore(workspace.id); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not restore workspace"); }
    finally { setRestoringId(null); }
  };
  const hasRunningSession = (workspaceId: string) => sessions.some((session) => session.workspace_id === workspaceId && session.state === "running");
  return <div className="page"><div className="page-heading"><div><p className="eyebrow">Durable shared workspaces</p><h1>File assets that outlive sessions</h1><p>Each workspace receives a managed volume. Launching another session mounts the same files again.</p></div><div className="button-group"><button className="icon-button workspace-usage-refresh" aria-label="Refresh workspace usage" title="Refresh file count and storage usage" onClick={() => void refreshUsage()} disabled={usageLoading}>{usageLoading ? "…" : "↻"}</button><button className="secondary-button" onClick={onCreateWorkspace}>＋ Add workspace</button><button className="primary-button" disabled={!workspaces.length} onClick={() => onLaunch()}>＋ New session</button></div></div>{message && <p className="dialog-error workspace-message">{message}</p>}{!workspaces.length && <section className="empty-state"><strong>No active workspace</strong><p>Create a durable workspace before launching an agent session, or restore one from trash.</p><button className="primary-button" onClick={onCreateWorkspace}>Create workspace</button></section>}<div className="workspace-grid">{workspaces.map((workspace) => { const running = hasRunningSession(workspace.id); const stats = usage[workspace.id]; const usageLabel = stats ? (stats.available ? formatStorage(stats.storage_bytes) : "Unavailable") : usageLoading ? "Loading…" : "Not measured"; return <article className="workspace-card" key={workspace.id}><div className="workspace-icon">▣</div><p className="eyebrow">{workspace.kind}</p><h2>{workspace.name}</h2><p className="workspace-description">{workspace.description || "No description provided."}</p><span className={`workspace-status ${workspace.status === "ready" ? "ready" : "pending"}`}>{workspace.status}</span><dl><div><dt>Folder</dt><dd>{workspace.folder_name}</dd></div><div><dt>Files</dt><dd>{stats?.available ? stats.file_count?.toLocaleString() : "—"}</dd></div><div><dt>Storage used</dt><dd>{usageLabel}</dd></div><div><dt>Attached agents</dt><dd>{workspace.attached_sessions}</dd></div><div><dt>Last opened</dt><dd>{workspace.last_opened}</dd></div></dl><div className="workspace-card-actions"><WorkspaceIdeControl sessions={sessions.filter((session) => session.workspace_id === workspace.id && session.state === "running")} onLaunch={() => onLaunch(workspace.id)} onOpenIde={onOpenIde} /><button className="danger-button workspace-delete" disabled={running} title={running ? "Stop running sessions before deleting this workspace" : undefined} onClick={() => setPendingDelete(workspace)}>{running ? "Stop running sessions to delete" : "Delete workspace…"}</button></div></article>; })}</div>{deletedWorkspaces.length > 0 && <section className="trash-section"><div className="panel-head"><div><p className="eyebrow">Workspace trash</p><h2>{deletedWorkspaces.length} recoverable workspace{deletedWorkspaces.length === 1 ? "" : "s"}</h2></div></div><p>Soft-deleted workspaces keep all files and completed session history until you restore or permanently delete them.</p><div className="workspace-grid">{deletedWorkspaces.map((workspace) => { const stats = usage[workspace.id]; const usageLabel = stats ? (stats.available ? formatStorage(stats.storage_bytes) : "Unavailable") : usageLoading ? "Loading…" : "Not measured"; return <article className="workspace-card deleted-workspace" key={workspace.id}><div className="workspace-icon">▣</div><p className="eyebrow">Deleted {workspace.deleted_at ? formatDisplayTime(workspace.deleted_at, undefined, false) : ""}</p><h2>{workspace.name}</h2><p className="workspace-description">{workspace.description || "No description provided."}</p><dl><div><dt>Folder</dt><dd>{workspace.folder_name}</dd></div><div><dt>Files</dt><dd>{stats?.available ? stats.file_count?.toLocaleString() : "—"}</dd></div><div><dt>Storage used</dt><dd>{usageLabel}</dd></div><div><dt>Completed history</dt><dd>Retained</dd></div></dl><div className="workspace-card-actions"><button className="success-button" disabled={restoringId === workspace.id} onClick={() => void restore(workspace)}>{restoringId === workspace.id ? "Restoring…" : "Restore workspace"}</button><button className="danger-button workspace-delete" onClick={() => setPendingDelete(workspace)}>Permanently delete…</button></div></article>; })}</div></section>}{pendingDelete && <WorkspaceDeleteDialog workspace={pendingDelete} onClose={() => setPendingDelete(null)} onDelete={remove} />}</div>;
}

function WorkspaceDeleteDialog({ workspace, onClose, onDelete }: { workspace: Workspace; onClose: () => void; onDelete: (workspace: Workspace, mode: "soft" | "hard") => Promise<void> }) {
  const [deleting, setDeleting] = useState<"soft" | "hard" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const choose = async (mode: "soft" | "hard") => {
    setDeleting(mode); setMessage(null);
    try { await onDelete(workspace, mode); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not delete workspace"); }
    finally { setDeleting(null); }
  };
  const trashed = Boolean(workspace.deleted_at);
  return <div className="modal-backdrop confirmation-backdrop" role="presentation"><section className="workspace-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="workspace-delete-title"><div className="confirmation-icon">!</div><div><p className="eyebrow">Workspace deletion</p><h2 id="workspace-delete-title">{trashed ? "Permanently delete workspace?" : `Delete ${workspace.name}?`}</h2><p>{trashed ? "This permanently deletes the workspace files from its configured storage. This action cannot be undone." : "Choose whether to retain the workspace in trash for recovery or permanently remove its files."}</p></div>{message && <p className="dialog-error">{message}</p>}<div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose} disabled={deleting !== null}>Cancel</button>{!trashed && <button type="button" className="secondary-button" onClick={() => void choose("soft")} disabled={deleting !== null}>{deleting === "soft" ? "Moving…" : "Move to trash"}</button>}<button type="button" className="danger-button confirmation-action" onClick={() => void choose("hard")} disabled={deleting !== null}>{deleting === "hard" ? "Deleting…" : "Permanently delete"}</button></div></section></div>;
}

function WorkspaceIdeControl({ sessions, onLaunch, onOpenIde }: { sessions: Session[]; onLaunch: () => void; onOpenIde: (sessionId: string) => void }) {
  const [sessionId, setSessionId] = useState("");
  if (!sessions.length) return <button className="secondary-button workspace-ide-hint" onClick={onLaunch}>Launch a session to view workspace</button>;
  if (sessions.length === 1) return <button className="secondary-button workspace-ide-open" onClick={() => onOpenIde(sessions[0].id)}>Open VS Code ↗</button>;
  return <div className="workspace-ide-control"><select aria-label="Choose a running session for VS Code" value={sessionId} onChange={(event) => setSessionId(event.target.value)}><option value="">Choose a running session…</option>{sessions.map((session) => <option key={session.id} value={session.id}>{session.name} · {session.harness}</option>)}</select><button className="secondary-button" disabled={!sessionId} onClick={() => onOpenIde(sessionId)}>Open VS Code ↗</button></div>;
}

function TerminalView({ sessions, session, onSelect }: { sessions: Session[]; session?: Session; onSelect: (id: string) => void }) {
  const runnable = sessions.filter((candidate) => candidate.state === "running");
  const active = runnable.find((candidate) => candidate.id === session?.id) ?? runnable[0];
  return <div className="page terminal-page"><div className="page-heading"><div><p className="eyebrow">Interactive terminal</p><h1>{active?.name ?? "No running session"}</h1><p>Choose a running session to attach its persistent harness terminal. Output and errors are retained for reconnects.</p></div><div className="terminal-selector"><label htmlFor="terminal-session">Session</label><select id="terminal-session" value={active?.id ?? ""} onChange={(event) => onSelect(event.target.value)} disabled={!runnable.length}>{!runnable.length && <option value="">No running sessions</option>}{runnable.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name} · {candidate.workspace}</option>)}</select>{active && <Status state={active.state} />}</div></div>{active ? <LiveTerminal key={active.id} session={active} /> : <section className="empty-state"><strong>No running session</strong><p>Launch or resume a workspace session, then select it here to open its terminal.</p></section>}</div>;
}

function SessionLogsView({ sessions, session, timezone, onSelect }: { sessions: Session[]; session?: Session; timezone?: string | null; onSelect: (id: string) => void }) {
  const active = sessions.find((candidate) => candidate.id === session?.id) ?? sessions[0];
  const [conversation, setConversation] = useState<ConversationMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [liveOutput, setLiveOutput] = useState<SessionOutput | null>(null);
  useEffect(() => {
    if (!active) { setConversation([]); return; }
    let current = true;
    const refresh = async () => { try { const next = await api.sessionConversation(active.id); if (current) { setConversation(next); setError(null); } } catch { if (current) setError("Unable to load interaction history."); } };
    void refresh();
    const timer = active.state === "running" ? window.setInterval(() => void refresh(), 3000) : undefined;
    return () => { current = false; if (timer) window.clearInterval(timer); };
  }, [active?.id, active?.state]);
  useEffect(() => {
    if (!active || active.state !== "running") { setLiveOutput(null); return; }
    let current = true;
    const refresh = async () => { try { const next = await api.sessionOutput(active.id); if (current) setLiveOutput(next); } catch { if (current) setLiveOutput(null); } };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { current = false; window.clearInterval(timer); };
  }, [active?.id, active?.state]);
  if (!active) return <div className="page"><section className="empty-state"><strong>No agent sessions</strong><p>Session logs appear after a session is launched.</p></section></div>;
  const liveText = liveOutput?.text || liveOutput?.message || "Waiting for the persistent terminal to produce output…";
  return <div className="page logs-page"><div className="page-heading"><div><p className="eyebrow">Session history</p><h1>{active.name}</h1><p>Durable user and agent interaction, with a separate live terminal view for in-progress work.</p></div><div className="terminal-selector"><label htmlFor="log-session">Session</label><select id="log-session" value={active.id} onChange={(event) => onSelect(event.target.value)}>{sessions.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name} · {candidate.workspace}</option>)}</select><a className="secondary-button log-download" href={`/api/v1/sessions/${active.id}/logs/download`} target="_blank" rel="noreferrer">Download raw log</a></div></div><section className="conversation-list">{error ? <p className="dialog-error">{error}</p> : conversation.length ? conversation.map((message, index) => <article className={`conversation-message ${message.role}`} key={`${message.occurred_at}-${index}`}><div><strong>{message.role === "user" ? "You" : message.role === "assistant" ? "Agent" : "Model/API error"}</strong><time>{formatDisplayTime(message.occurred_at, timezone)}</time></div><pre>{message.text}</pre></article>) : <section className="empty-state"><strong>No interaction recorded yet</strong><p>Harness-native messages appear here as a durable transcript. The live screen below still shows current progress.</p></section>}</section>{active.state === "running" && <section className="agent-output live-agent-output"><div className="agent-output-head"><div><p className="eyebrow">Live agent screen</p><small>{liveOutput ? `Captured ${formatDisplayTime(liveOutput.captured_at, timezone, false)}` : "Connecting to persistent terminal…"}</small></div><span>↻ every 3s</span></div><pre aria-live="polite">{liveText}</pre></section>}</div>;
}

function ModelsView({ models, onCreated }: { models: RegisteredModel[]; onCreated: () => Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<RegisteredModel | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [provider, setProvider] = useState("OpenRouter");
  const [endpoint, setEndpoint] = useState("https://openrouter.ai/api/v1");
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const formElement = event.currentTarget; const form = new FormData(formElement); setSaving(true); setMessage(null);
    const rate = (name: string) => { const value = String(form.get(name) || "").trim(); return value ? Number(value) : null; };
    try { await api.registerModel({ provider: String(form.get("provider")), display_name: String(form.get("display_name")), model_name: String(form.get("model_name")), endpoint: String(form.get("endpoint")), reasoning_effort: String(form.get("reasoning_effort")) as RegisteredModel["reasoning_effort"], input_cost_per_million: rate("input_cost_per_million"), output_cost_per_million: rate("output_cost_per_million"), cached_input_cost_per_million: rate("cached_input_cost_per_million"), api_key: String(form.get("api_key") || "") }); formElement.reset(); setProvider("OpenRouter"); setEndpoint("https://openrouter.ai/api/v1"); await onCreated(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not register model"); }
    finally { setSaving(false); }
  };
  const remove = async (model: RegisteredModel) => {
    setDeletingId(model.id); setMessage(null);
    try { await api.deleteModel(model.id); await onCreated(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not delete model"); }
    finally { setDeletingId(null); }
  };
  return <div className="page models-page"><div className="page-heading"><div><p className="eyebrow">Model registry & credential vault</p><h1>Registered provider models</h1><p>Keys and pricing are optional. Token rates let AgentCC estimate per-session cost.</p></div></div><div className="models-layout"><section className="panel model-form"><div className="panel-head"><div><p className="eyebrow">Add provider model</p><h2>{provider === "Local gateway" ? "Local model gateway" : "OpenRouter endpoint"}</h2></div></div><form onSubmit={(event) => void submit(event)}><label>Provider<select name="provider" value={provider} onChange={(event) => { const next = event.target.value; setProvider(next); setEndpoint(next === "Local gateway" ? "http://localhost:11434/v1" : "https://openrouter.ai/api/v1"); }}><option value="OpenRouter">OpenRouter</option><option value="Local gateway">Local gateway</option></select></label><label>Display name<input name="display_name" required placeholder={provider === "Local gateway" ? "e.g. Qwen 3 Coder on Ollama" : "e.g. Qwen 3 Coder via OpenRouter"} /></label><label>Model name<input name="model_name" required placeholder={provider === "Local gateway" ? "e.g. qwen3-coder:latest" : "e.g. qwen/qwen3-coder"} /></label><label>API endpoint<input name="endpoint" required type="url" value={endpoint} onChange={(event) => setEndpoint(event.target.value)} /></label>{provider === "Local gateway" && <p className="dialog-note">Use the host address or <code>localhost</code>; AgentCC maps loopback endpoints into session containers. Codex needs OpenAI Responses, Hermes/Kilo need OpenAI-compatible, and Claude Code needs an Anthropic Messages-compatible gateway.</p>}<label>Reasoning effort<select name="reasoning_effort" defaultValue="medium"><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="xhigh">Extra high</option></select></label><div className="pricing-fields"><p className="eyebrow">Pricing · USD per million tokens</p><label>Input<input name="input_cost_per_million" type="number" min="0" step="any" inputMode="decimal" placeholder="e.g. 2.50" /></label><label>Output<input name="output_cost_per_million" type="number" min="0" step="any" inputMode="decimal" placeholder="e.g. 10.00" /></label><label>Cached input<input name="cached_input_cost_per_million" type="number" min="0" step="any" inputMode="decimal" placeholder="e.g. 0.25" /></label><small>Leave a rate blank when the provider does not publish it. Estimates appear once every token category used by a session has a rate.</small></div><label>API key <small>(optional)</small><input name="api_key" type="password" autoComplete="off" placeholder="Stored encrypted; shown only as last four" /></label>{message && <p className="dialog-error">{message}</p>}<div className="dialog-actions"><button className="primary-button" disabled={saving}>{saving ? "Registering…" : "Register model"}</button></div></form></section><section className="panel model-list"><div className="panel-head"><div><p className="eyebrow">Available models</p><h2>{models.length} registered</h2></div></div>{models.length ? models.map((model) => <article className="model-row" key={model.id}><div className="model-identity"><strong>{model.display_name}</strong><small>{model.model_name}</small></div><div className="model-connection"><div><span className="provider-chip">{model.provider}</span><strong>{model.api_key_last_four ? `•••• ${model.api_key_last_four}` : "No API key"}</strong></div><small>{model.reasoning_effort} effort · {model.endpoint}</small><small>Input {formatRate(model.input_cost_per_million)} · Output {formatRate(model.output_cost_per_million)} · Cached {formatRate(model.cached_input_cost_per_million)}</small></div><button className="danger-button model-delete" disabled={deletingId === model.id} onClick={() => setPendingDelete(model)}>{deletingId === model.id ? "Deleting…" : "Delete"}</button></article>) : <div className="empty-state"><strong>No provider model registered</strong><p>Add a supported provider model and endpoint to make it selectable when launching a session.</p></div>}</section></div>{pendingDelete && <ConfirmDialog request={{ title: "Delete registered model?", message: `${pendingDelete.display_name}'s encrypted credential will be removed too. Sessions using it cannot be launched again until another model is selected.`, confirmLabel: "Delete model", onConfirm: async () => { await remove(pendingDelete); } }} onClose={() => setPendingDelete(null)} />}</div>;
}

function SettingsView({ workspaceStorage, displaySettings, onSaved }: { workspaceStorage: WorkspaceStorageSettings; displaySettings: DisplaySettings; onSaved: () => Promise<void> }) {
  const [selectedRoot, setSelectedRoot] = useState(workspaceStorage.workspace_host_root ?? "");
  const [selectedTimezone, setSelectedTimezone] = useState(displaySettings.timezone ?? "");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => setSelectedRoot(workspaceStorage.workspace_host_root ?? ""), [workspaceStorage.workspace_host_root]);
  useEffect(() => setSelectedTimezone(displaySettings.timezone ?? ""), [displaySettings.timezone]);
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setSaving(true); setMessage(null);
    try { await Promise.all([api.updateWorkspaceStorageSettings({ workspace_host_root: selectedRoot || null }), api.updateDisplaySettings({ timezone: selectedTimezone.trim() || null })]); await onSaved(); setMessage("Settings saved."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not save settings"); }
    finally { setSaving(false); }
  };
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return <div className="page settings-page"><div className="page-heading"><div><p className="eyebrow">Settings & config</p><h1>Storage and display preferences</h1><p>Workspace storage affects only new workspaces. Timestamps always remain stored in UTC and are rendered in the selected display timezone.</p></div></div><section className="panel settings-form"><div className="panel-head"><div><p className="eyebrow">Durable assets</p><h2>Host workspace root</h2></div></div><form onSubmit={(event) => void save(event)}><label>Storage location<select value={selectedRoot} onChange={(event) => setSelectedRoot(event.target.value)}><option value="">Docker-managed volume</option>{workspaceStorage.available_workspace_host_roots.map((root) => <option key={root} value={root}>{root}</option>)}</select></label><p className="dialog-note">AgentCC creates one UUID-named directory per new workspace beneath this root. The directory must be accessible to the Docker daemon. Add or remove approved locations in the local runtime Compose configuration.</p><div className="panel-head settings-subhead"><div><p className="eyebrow">Timestamp display</p><h2>Timezone</h2></div></div><label>Display timezone<input list="timezone-options" value={selectedTimezone} onChange={(event) => setSelectedTimezone(event.target.value)} placeholder={`Browser local time (${browserTimezone})`} /></label><datalist id="timezone-options"><option value="UTC" /><option value="America/Los_Angeles" /><option value="America/Denver" /><option value="America/Chicago" /><option value="America/New_York" /><option value="Europe/London" /><option value="Europe/Berlin" /><option value="Asia/Tokyo" /><option value="Asia/Singapore" /><option value="Australia/Sydney" /></datalist><p className="dialog-note">Leave blank to use this browser’s local timezone. Or enter a valid IANA timezone, such as <code>America/Los_Angeles</code>; all session logs will be displayed in it.</p>{message && <p className="dialog-error">{message}</p>}<div className="dialog-actions"><button className="primary-button" disabled={saving}>{saving ? "Saving…" : "Save settings"}</button></div></form></section></div>;
}

function LiveTerminal({ session }: { session: Session }) {
  const host = useRef<HTMLDivElement>(null);
  const copySelection = useRef<() => void>(() => undefined);
  const pasteClipboard = useRef<() => void>(() => undefined);
  const copyResetTimer = useRef<number | null>(null);
  const [connection, setConnection] = useState("Connecting…");
  const [copyStatus, setCopyStatus] = useState("Shift-drag to select text");
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    let disposed = false;
    let cleanup: (() => void) | undefined;
    const connect = async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")]);
      if (disposed || !host.current) return;
      const terminal = new Terminal({
        cursorBlink: true,
        fontSize: 13,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        // Keep enough history for an operator to review prior agent output
        // without relying on the visible tmux pane alone.
        scrollback: 10_000,
        scrollOnUserInput: true,
        theme: { background: "#0a0e14", foreground: "#dfe2eb", cursor: "#7bd0ff", selectionBackground: "rgba(56, 189, 248, 0.38)", selectionInactiveBackground: "rgba(56, 189, 248, 0.24)" }
      });
      const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(host.current); terminal.focus();
      const copy = () => {
        const copied = () => {
          setCopyStatus("Copied");
          if (copyResetTimer.current !== null) window.clearTimeout(copyResetTimer.current);
          copyResetTimer.current = window.setTimeout(() => setCopyStatus("Shift-drag to select text"), 2000);
        };
        const text = terminal.getSelection();
        if (!text) { setCopyStatus("Select text first"); return; }
        if (navigator.clipboard?.writeText) {
          void navigator.clipboard.writeText(text).then(
            copied,
            () => setCopyStatus("Clipboard permission was denied"),
          );
          return;
        }
        const fallback = document.createElement("textarea");
        fallback.value = text;
        fallback.style.position = "fixed";
        fallback.style.opacity = "0";
        document.body.append(fallback);
        fallback.select();
        const copiedWithFallback = document.execCommand("copy");
        fallback.remove();
        if (copiedWithFallback) copied(); else setCopyStatus("Clipboard permission was denied");
      };
      copySelection.current = copy;
      pasteClipboard.current = () => {
        if (!navigator.clipboard?.readText) { setCopyStatus("Clipboard paste is unavailable in this browser"); return; }
        void navigator.clipboard.readText().then(
          (text) => {
            if (!text) { setCopyStatus("Clipboard is empty"); return; }
            terminal.paste(text);
            setCopyStatus("Clipboard pasted into terminal");
          },
          () => setCopyStatus("Clipboard permission was denied"),
        );
      };
      terminal.attachCustomKeyEventHandler((event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c" && terminal.hasSelection()) {
          event.preventDefault();
          copy();
          return false;
        }
        return true;
      });
    let socket: WebSocket | undefined;
    const fitTerminal = () => {
      fit.fit();
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
    };
    requestAnimationFrame(fitTerminal);
    let active = true;
    void api.sessionLogs(session.id).then((logs) => { if (active) logs.forEach((log) => terminal.write(log.text)); }).catch(() => terminal.write("\r\n\x1b[31mUnable to load retained session logs.\x1b[0m\r\n"));
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${protocol}://${window.location.host}/api/v1/sessions/${session.id}/terminal`);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => { setConnection("Connected"); requestAnimationFrame(fitTerminal); };
    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        try {
          const message = JSON.parse(event.data) as { type?: string; message?: string };
          if (message.type === "error") terminal.write(`\r\n\x1b[31mTerminal error: ${message.message ?? "Unknown error"}\x1b[0m\r\n`);
          if (message.type === "notice") {
            const text = message.message ?? "A replacement harness session was started.";
            setNotice(text);
            terminal.write(`\r\n\x1b[33mNotice: ${text}\x1b[0m\r\n`);
          }
        } catch { terminal.write(event.data); }
      } else { terminal.write(new Uint8Array(event.data)); }
    };
    socket.onerror = () => { setConnection("Connection error"); terminal.write("\r\n\x1b[31mTerminal connection failed. See retained logs above.\x1b[0m\r\n"); };
    socket.onclose = () => setConnection("Disconnected");
    const input = terminal.onData((data) => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data })); });
    const resize = new ResizeObserver(() => requestAnimationFrame(fitTerminal));
    resize.observe(host.current);
      cleanup = () => { active = false; if (copyResetTimer.current !== null) window.clearTimeout(copyResetTimer.current); copySelection.current = () => undefined; pasteClipboard.current = () => undefined; resize.disconnect(); input.dispose(); socket.close(); terminal.dispose(); };
    };
    void connect().catch(() => setConnection("Terminal failed to load"));
    return () => { disposed = true; cleanup?.(); };
  }, [session.id]);
  return <section className="terminal"><div className="terminal-tabs"><span>persistent {session.harness.toLowerCase()} tmux</span><span>{connection}</span><button type="button" className="terminal-copy" onClick={() => copySelection.current()}>{copyStatus === "Copied" ? "Copied" : "Copy selected text"}</button><button type="button" className="terminal-copy" onClick={() => pasteClipboard.current()}>Paste from clipboard</button><span className="terminal-scroll-hint">Scroll for tmux history · press q to return live</span><span className="terminal-copy-status">{copyStatus}</span></div>{notice && <div className="terminal-replacement-notice" role="alert">{notice}</div>}<div className="xterm-host" ref={host} aria-label="Interactive terminal. Scroll to browse tmux history; press q to return to the live pane." /></section>;
}

function PlaceholderView({ view }: { view: View }) {
  return <div className="page placeholder"><p className="eyebrow">MVP foundation</p><h1>{view === "models" ? "Model registry & credential vault" : "Preferences & environment settings"}</h1><p>This command surface is reserved and will use the API contract defined in the system design. Fleet, workspace, and lifecycle controls are live first.</p></div>;
}

function Inspector({ session, onAction, onDelete }: { session?: Session; onAction: (action: "suspend" | "resume" | "stop") => Promise<void>; onDelete: () => void }) {
  const [output, setOutput] = useState<SessionOutput | null>(null);
  const [outputError, setOutputError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    if (!session) {
      setOutput(null);
      setOutputError(null);
      setStopping(false);
      return;
    }
    let active = true;
    const refreshOutput = async () => {
      try {
        const next = await api.sessionOutput(session.id);
        if (active) {
          setOutput(next);
          setOutputError(null);
        }
      } catch {
        if (active) setOutputError("Unable to refresh agent output.");
      }
    };
    void refreshOutput();
    const timer = window.setInterval(() => void refreshOutput(), 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [session?.id, session?.state]);

  if (!session) return <div className="inspector-empty">Select a session to inspect it.</div>;
  const resume = session.state === "suspended" || session.state === "idle";
  const canDelete = session.state === "completed";
  const outputText = outputError || output?.text || output?.message || "Waiting for agent output…";
  const activity = output?.agent_activity ?? session.agent_activity;
  const stop = async () => { setStopping(true); try { await onAction("stop"); } finally { setStopping(false); } };
  return <><div className="inspector-head"><p className="eyebrow">Execution inspector</p><h2>{session.name}</h2><Status state={session.state} />{session.state === "running" && <ActivityStatus activity={activity} />}</div><div className="inspector-section"><p className="eyebrow">Target</p><strong>{session.workspace}</strong><small>{session.task}</small></div><div className="inspector-section metrics-list"><Metric label="Harness" value={session.harness} /><Metric label="Model" value={session.model} /><Metric label="Agent status" value={activityLabel(activity)} /><Metric label="Session duration" value={formatDuration(session)} /><Metric label="Total tokens" value={formatTokens(session.tokens)} /><Metric label="Model calls" value={String(session.model_calls)} /><Metric label="Input / output" value={`${formatTokens(session.input_tokens)} / ${formatTokens(session.output_tokens)}`} /><Metric label="Cached input" value={formatTokens(session.cached_input_tokens)} /><Metric label="Estimated cost" value={formatCost(session)} /></div><div className="inspector-actions">{resume ? <button className="success-button" disabled={stopping} onClick={() => void onAction("resume")}>▶ Resume</button> : <button className="secondary-button" disabled={stopping} onClick={() => void onAction("suspend")}>Ⅱ Suspend</button>}<button className="danger-button" disabled={stopping} onClick={() => void stop()}>{stopping ? "Stopping…" : "■ Stop"}</button><button className="danger-button delete-session" onClick={onDelete} disabled={!canDelete || stopping} title={canDelete ? "Delete completed session" : "Stop the session before deleting it"}>Delete session</button></div><div className="inspector-section agent-output"><div className="agent-output-head"><p className="eyebrow">Latest agent output</p><span>↻ every 3s</span></div><pre aria-live="polite">{outputText}</pre></div><p className="inspector-note">Activity is inferred from the persistent harness pane and process state. This is a read-only terminal tail, so it continues updating even when no browser terminal is attached. Stop a session before deleting its container, private session files, metadata, and retained logs. The durable workspace remains.</p></>;
}

function Status({ state }: { state: Session["state"] }) {
  return <span className={`status ${state}`}><i />{stateLabel(state)}</span>;
}

function ActivityStatus({ activity }: { activity: AgentActivity }) {
  return <span className={`activity-status ${activity}`}><i />{activityLabel(activity)}</span>;
}

function LaunchDialog({ workspaces, models, initialWorkspaceId, onClose, onCreated }: { workspaces: Workspace[]; models: RegisteredModel[]; initialWorkspaceId: string | null; onClose: () => void; onCreated: () => Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [harness, setHarness] = useState<"Codex" | "Hermes" | "Claude Code" | "Kilo Code">("Codex");
  const [workspaceId, setWorkspaceId] = useState(initialWorkspaceId ?? "");
  const requiresRegisteredModel = harness === "Hermes" || harness === "Kilo Code";
  const defaultModelLabel = harness === "Claude Code" ? "Default Claude model" : "Default OpenAI model";
  useEffect(() => setWorkspaceId(initialWorkspaceId ?? ""), [initialWorkspaceId]);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setSaving(true); setMessage(null);
    const modelId = String(form.get("model_id") || "");
    const selectedModel = models.find((model) => model.id === modelId);
    try { await api.createSession({ name: String(form.get("name")), workspace_id: String(form.get("workspace")), task: String(form.get("task")), harness, model: selectedModel?.model_name ?? defaultModelLabel, model_id: selectedModel?.id ?? null }); await onCreated(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not launch session"); }
    finally { setSaving(false); }
  };
  return <div className="modal-backdrop" role="presentation"><form className="launch-dialog" onSubmit={(event) => void submit(event)}><div className="panel-head"><div><p className="eyebrow">Launch agent</p><h2>New {harness} session</h2></div><button type="button" className="icon-button" onClick={onClose}>×</button></div><label>Session name<input name="name" required placeholder="e.g. api-test-hardening" /></label><label>Harness<select name="harness" value={harness} onChange={(event) => setHarness(event.target.value as "Codex" | "Hermes" | "Claude Code" | "Kilo Code")}><option value="Codex">Codex</option><option value="Hermes">Hermes</option><option value="Claude Code">Claude Code</option><option value="Kilo Code">Kilo Code</option></select></label><label>Durable workspace<select name="workspace" value={workspaceId || workspaces[0]?.id || ""} onChange={(event) => setWorkspaceId(event.target.value)}>{workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></label><label>Model<select name="model_id" required={requiresRegisteredModel}><option value="">{harness === "Claude Code" ? "Default Claude Code settings (container sign-in)" : requiresRegisteredModel ? "Select a registered model" : "Codex default (container sign-in)"}</option>{models.filter((model) => model.enabled).map((model) => <option key={model.id} value={model.id}>{model.display_name} · {model.provider}</option>)}</select></label>{harness === "Claude Code" && <p className="dialog-note">Choose Default settings for Claude Code’s native login/model flow, or select an OpenRouter or Anthropic-compatible Local gateway model.</p>}{harness === "Kilo Code" && <p className="dialog-note">Kilo Code uses the selected OpenRouter or OpenAI-compatible Local gateway model and starts in auto-approve mode inside this isolated session container.</p>}<label>Initial task <span className="field-optional">(optional)</span><textarea name="task" placeholder="Start the harness without sending an initial prompt" rows={4} /></label>{message && <p className="dialog-error">{message}</p>}<div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button className="primary-button" disabled={saving || !workspaces.length}>{saving ? "Launching…" : "Launch session"}</button></div></form></div>;
}

function WorkspaceDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setSaving(true); setMessage(null);
    try { await api.createWorkspace({ name: String(form.get("name")), description: String(form.get("description")) }); await onCreated(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not create workspace"); }
    finally { setSaving(false); }
  };
  return <div className="modal-backdrop" role="presentation"><form className="launch-dialog" onSubmit={(event) => void submit(event)}><div className="panel-head"><div><p className="eyebrow">Workspace storage</p><h2>Add durable workspace</h2></div><button type="button" className="icon-button" onClick={onClose}>×</button></div><p className="dialog-note">Files in this managed volume survive when any attached session is stopped.</p><label>Name<input name="name" required minLength={2} placeholder="e.g. product-assets" /></label><label>Description<textarea name="description" placeholder="What belongs in this shared workspace?" rows={3} /></label>{message && <p className="dialog-error">{message}</p>}<div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button className="primary-button" disabled={saving}>{saving ? "Creating…" : "Create workspace"}</button></div></form></div>;
}
