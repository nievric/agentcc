import { useEffect, useState } from "react";
import { api, CheckoutRequest, RepositoryInfo, Session, Worktree, waitForOperation } from "./api";

export function CheckoutSelector({ workspaceId, initialWorktreeId, onChange }: { workspaceId: string; initialWorktreeId?: string | null; onChange: (value: CheckoutRequest | null) => void }) {
  const [repository, setRepository] = useState<RepositoryInfo | null>(null);
  const [tasks, setTasks] = useState<Worktree[]>([]);
  const [mode, setMode] = useState<CheckoutRequest["mode"] | "">("");
  const [branch, setBranch] = useState("");
  const [taskId, setTaskId] = useState("");
  const [name, setName] = useState("");
  const [committed, setCommitted] = useState(false);
  const [reuseIdentity, setReuseIdentity] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    onChange(null); setRepository(null); setMode(""); setError(null); setCommitted(false); setName("");
    if (workspaceId) {
      void Promise.all([api.repository(workspaceId), api.worktrees(workspaceId)]).then(([info, list]) => {
        if (!active) return;
        setRepository(info); setTasks(list); setBranch(info.branch ?? "");
        setMode(initialWorktreeId ? "existing_worktree" : info.available ? "new_worktree" : "shared"); setTaskId(initialWorktreeId ?? "");
      }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "Could not inspect repository"); });
    }
    return () => { active = false; };
  }, [workspaceId]);
  useEffect(() => {
    if (!mode || (mode === "existing_worktree" && !taskId)) { onChange(null); return; }
    if (mode === "new_worktree") {
      const commit = branch ? repository?.branches[branch] : repository?.head;
      onChange(commit ? { mode, source_branch: branch || undefined, expected_base_commit: commit, branch: name || undefined, use_committed_version: committed, reuse_git_identity: reuseIdentity } : null);
    } else onChange({ mode, ...(mode === "existing_worktree" ? { worktree_id: taskId } : {}) });
  }, [mode, branch, taskId, name, committed, reuseIdentity, repository]);
  const reusable = tasks.filter((task) => task.state === "ready" && !task.reserved_session_id);
  return <fieldset className="checkout-options"><legend>Agent files</legend>
    {!repository && !error && <p>Checking repository…</p>}
    {error && <p role="alert" className="dialog-error">{error} Choose the original workspace explicitly to continue there.</p>}
    <label>Working files<select aria-label="Working files" value={mode} onChange={(event) => setMode(event.target.value as CheckoutRequest["mode"])}>
      {!mode && <option value="">Choose working files…</option>}
      <option value="new_worktree" disabled={!repository?.available}>Separate branch (recommended)</option>
      <option value="existing_worktree" disabled={!reusable.length}>Continue an existing task</option>
      <option value="shared">Use original workspace</option>
    </select></label>
    {mode === "new_worktree" && <><p>Give this agent its own files and branch. Other tasks keep their changes.</p>
      <label>Start from<select value={branch} onChange={(event) => setBranch(event.target.value)}>{!repository?.branch && <option value="">Current commit</option>}{Object.keys(repository?.branches ?? {}).map((item) => <option key={item}>{item}</option>)}</select></label>
      <p>Starting commit: <code>{(branch ? repository?.branches[branch] : repository?.head)?.slice(0, 8)}</code></p>
      {repository?.dirty && <label className="checkbox-label"><input type="checkbox" required checked={committed} onChange={(event) => setCommitted(event.target.checked)} />Use committed version. Local edits in the original workspace will not be included.</label>}
      <details><summary>Advanced branch options</summary><label>Branch name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Generate automatically from the task name" /></label><p>A Git worktree is a separate checkout with shared history. Dependencies and ignored files are not copied.</p></details>
      {repository?.author_name && repository.author_email ? <label className="checkbox-label"><input type="checkbox" checked={reuseIdentity} onChange={(event) => setReuseIdentity(event.target.checked)} />Use {repository.author_name} &lt;{repository.author_email}&gt; as the commit author for this workspace’s branch tasks.</label> : <p>Configure a commit author in the task’s VS Code terminal before committing: <code>git config user.name</code> and <code>git config user.email</code>.</p>}
    </>}
    {mode === "existing_worktree" && <label>Branch task<select value={taskId} onChange={(event) => setTaskId(event.target.value)}><option value="">Select a task…</option>{reusable.map((task) => <option value={task.id} key={task.id}>{task.name} · {task.branch}</option>)}</select></label>}
    {mode === "shared" && <p>{repository?.reason ?? "This agent uses the original workspace files. Other sessions using those files can change them too."}</p>}
  </fieldset>;
}

export function BranchTasks({ workspaceId, sessions, onLaunch, onOpenIde, disabled = false }: { workspaceId: string; sessions: Session[]; onLaunch: (taskId: string) => void; onOpenIde: (id: string) => void; disabled?: boolean }) {
  const [tasks, setTasks] = useState<Worktree[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{ task: Worktree; action: "export" | "remove" } | null>(null);
  const refresh = async () => { setTasks(await api.worktrees(workspaceId)); };
  useEffect(() => {
    let active = true;
    const read = () => { void api.worktrees(workspaceId).then((list) => { if (active) setTasks(list); }).catch((error) => { if (active) setMessage(String(error.message)); }); };
    read(); const timer = window.setInterval(read, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [workspaceId]);
  const perform = async (job: () => Promise<unknown>) => {
    setBusy(true); setMessage(null);
    try { await job(); if (pending) setPending(null); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Task operation failed"); }
    finally { setBusy(false); }
  };
  if (!tasks.length && !message) return null;
  return <section className="branch-tasks" aria-label="Branch tasks"><h3>Branch tasks</h3>
    {message && <p role="alert" className="dialog-error">{message}</p>}
    {tasks.map((task) => {
      const session = sessions.find((item) => item.worktree_id === task.id && item.state !== "completed");
      const occupied = Boolean(session || task.reserved_session_id);
      return <article className="branch-task" key={task.id}><strong>{task.name}</strong><code>{task.branch}</code><small>{task.state}{task.dirty ? " · local files retained" : ""}{task.exported_commit ? ` · exported ${task.exported_commit.slice(0, 8)}` : ""}</small>
        {task.error && <p role="alert">{task.error}</p>}
        <div className="branch-task-actions">
          {["ready", "archived", "removed"].includes(task.state) && task.tip && <button disabled={busy} onClick={() => void perform(() => api.worktree(task.id))}>Refresh status</button>}
          {session?.state === "running" && <button className="secondary-button" onClick={() => onOpenIde(session.id)}>Open task in VS Code</button>}
          {task.state === "ready" && !occupied && <button className="secondary-button" disabled={disabled} onClick={() => onLaunch(task.id)}>Continue task…</button>}
          {!occupied && task.state === "ready" && <button disabled={busy} onClick={() => void perform(() => api.worktreeAction(task.id, "archive"))}>Archive</button>}
          {!occupied && task.state === "archived" && <button disabled={busy || disabled} onClick={() => void perform(() => api.worktreeAction(task.id, "restore"))}>Restore task</button>}
          {!occupied && ["ready", "archived", "removed"].includes(task.state) && <button disabled={busy} onClick={() => void perform(async () => { const updated = await api.worktree(task.id); setPending({ task: updated, action: "export" }); })}>Export branch…</button>}
          {!occupied && task.state !== "removed" && <button disabled={busy} onClick={() => setPending({ task, action: "remove" })}>Remove checkout…</button>}
          {task.error && task.operation_id && <button disabled={busy || disabled} onClick={() => void perform(async () => { await waitForOperation(await api.retryOperation(task.operation_id!)); })}>Retry launch</button>}
          {task.reserved_session_id && !session && <button disabled={busy} onClick={() => void perform(() => api.worktreeAction(task.id, "cancel-launch"))}>Cancel pending launch</button>}
        </div></article>;
    })}
    {pending && <div className="branch-confirm" role="alertdialog" aria-label={pending.action === "export" ? "Export branch" : "Remove checkout"}>
      <p>{pending.action === "export" ? `Make commit ${pending.task.tip?.slice(0, 8)} available as ${pending.task.branch} in the original workspace? This does not merge changes. Uncommitted files are not exported.` : "Remove this checkout after its sessions have been deleted? Its branch history will be retained. Modified, untracked, and ignored files block removal."}</p>
      <button disabled={busy} onClick={() => setPending(null)}>Cancel</button><button disabled={busy || (pending.action === "export" && !pending.task.tip)} onClick={() => void perform(() => pending.action === "export" ? api.exportWorktree(pending.task.id, pending.task.tip!) : api.worktreeAction(pending.task.id, "remove"))}>{pending.action === "export" ? "Export reviewed commit" : "Remove clean checkout"}</button>
    </div>}
  </section>;
}
