export type SessionState = "running" | "suspended" | "idle" | "completed";
export type AgentActivity = "working" | "waiting_for_input" | "not_started" | "paused" | "stopped" | "unknown";

export type Session = {
  id: string;
  workspace_id: string;
  name: string;
  workspace: string;
  harness: string;
  model: string;
  model_id?: string | null;
  state: SessionState;
  agent_activity: AgentActivity;
  task: string;
  tokens: number;
  cost_usd: number;
  cost_estimate_available: boolean;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  reasoning_tokens: number;
  model_calls: number;
  tool_calls: number;
  container_id?: string | null;
  volume_name?: string | null;
  worktree_id?: string | null;
  started_at: string;
  ended_at?: string | null;
};

export type Summary = {
  total_sessions: number;
  running: number;
  suspended: number;
  idle: number;
  tokens_last_hour: number;
  tokens_last_24_hours: number;
  tokens_last_7_days: number;
  estimated_cost_usd: number;
  estimated_cost_available: boolean;
  uptime_label: string;
};

export type Telemetry = {
  cpu_percent: number;
  memory_used_gb: number;
  memory_total_gb: number;
  active_sessions: number;
  session_tokens: number;
  latency_ms: number;
};

export type Workspace = {
  id: string;
  name: string;
  description: string;
  kind: string;
  status: string;
  attached_sessions: number;
  last_opened: string;
  volume_name: string;
  folder_name: string;
  host_path?: string | null;
  created_at: string;
  deleted_at?: string | null;
};

export type WorkspaceStorageSettings = {
  workspace_host_root?: string | null;
  available_workspace_host_roots: string[];
};

export type WorkspaceUsage = {
  workspace_id: string;
  file_count?: number | null;
  storage_bytes?: number | null;
  available: boolean;
};

export type DisplaySettings = { timezone?: string | null };

export type CheckoutRequest = {
  mode: "shared" | "new_worktree" | "existing_worktree";
  worktree_id?: string;
  source_branch?: string;
  expected_base_commit?: string;
  branch?: string;
  use_committed_version?: boolean;
  reuse_git_identity?: boolean;
};
export type RepositoryInfo = {
  available: boolean; reason?: string | null; head?: string | null;
  branch?: string | null; branches: Record<string, string>; dirty: boolean;
  author_name?: string | null; author_email?: string | null;
};
export type Worktree = {
  id: string; workspace_id: string; name: string; branch: string; base_commit: string;
  state: "creating" | "ready" | "archived" | "removing" | "removed" | "needs_attention";
  tip?: string | null; dirty?: boolean | null; exported_commit?: string | null;
  reserved_session_id?: string | null; operation_id?: string | null; error?: string | null;
};
export type Operation = {
  id: string; workspace_id: string; worktree_id: string; session_id?: string | null;
  state: "pending" | "running" | "completed" | "failed" | "cancelled"; error?: string | null;
};

export type SessionLog = {
  id: number;
  session_id: string;
  stream: string;
  text: string;
  occurred_at: string;
};

export type SessionLogTail = {
  text: string;
  available: boolean;
  truncated: boolean;
};

export type ConversationMessage = { role: "user" | "assistant" | "system"; text: string; occurred_at: string };
export type ReasoningEffort = "low" | "medium" | "high" | "xhigh";

export type SessionOutput = {
  text: string;
  available: boolean;
  message?: string | null;
  agent_activity: AgentActivity;
  captured_at: string;
};

export type RegisteredModel = {
  id: string;
  provider: string;
  display_name: string;
  model_name: string;
  endpoint: string;
  reasoning_effort: ReasoningEffort;
  input_cost_per_million?: number | null;
  output_cost_per_million?: number | null;
  cached_input_cost_per_million?: number | null;
  credential_label: string;
  api_key_last_four: string;
  enabled: boolean;
  created_at: string;
};

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

type ApiValidationIssue = { loc?: unknown; msg?: string };
type ApiProblem = { detail?: string | ApiValidationIssue[] };

function problemMessage(problem: ApiProblem | null, status: number): string {
  if (typeof problem?.detail === "string") return problem.detail;
  if (Array.isArray(problem?.detail)) {
    return problem.detail.map((issue) => {
      const location = Array.isArray(issue.loc)
        ? issue.loc.filter((part) => part !== "body").map(String).join(".")
        : "";
      return `${location || "Request"}: ${issue.msg ?? "Invalid value"}`;
    }).join(" ");
  }
  return `Request failed (${status})`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => null) as ApiProblem | null;
    throw new Error(problemMessage(problem, response.status));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  summary: () => request<Summary>("/system/summary"),
  telemetry: () => request<Telemetry>("/telemetry"),
  sessions: () => request<Session[]>("/sessions"),
  workspaces: () => request<Workspace[]>("/workspace-sessions"),
  deletedWorkspaces: () => request<Workspace[]>("/workspaces/deleted"),
  workspaceUsage: () => request<WorkspaceUsage[]>("/workspaces/usage"),
  workspaceStorageSettings: () => request<WorkspaceStorageSettings>("/settings/workspace-storage"),
  updateWorkspaceStorageSettings: (payload: Pick<WorkspaceStorageSettings, "workspace_host_root">) =>
    request<WorkspaceStorageSettings>("/settings/workspace-storage", { method: "PUT", body: JSON.stringify(payload) }),
  displaySettings: () => request<DisplaySettings>("/settings/display"),
  updateDisplaySettings: (payload: DisplaySettings) =>
    request<DisplaySettings>("/settings/display", { method: "PUT", body: JSON.stringify(payload) }),
  models: () => request<RegisteredModel[]>("/models"),
  registerModel: (payload: Pick<RegisteredModel, "provider" | "display_name" | "model_name" | "endpoint" | "reasoning_effort" | "input_cost_per_million" | "output_cost_per_million" | "cached_input_cost_per_million"> & { api_key?: string }) =>
    request<RegisteredModel>("/models", { method: "POST", body: JSON.stringify(payload) }),
  deleteModel: (id: string) => request<void>(`/models/${id}`, { method: "DELETE" }),
  createWorkspace: (payload: Pick<Workspace, "name" | "description">) =>
    request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify(payload) }),
  deleteWorkspace: (id: string, mode: "soft" | "hard", discard_task_history = false) =>
    request<void>(`/workspaces/${id}`, { method: "DELETE", body: JSON.stringify({ mode, discard_task_history }) }),
  restoreWorkspace: (id: string) => request<Workspace>(`/workspaces/${id}/restore`, { method: "POST" }),
  createSession: (payload: Pick<Session, "name" | "workspace_id" | "task" | "harness" | "model"> & { model_id?: string | null; checkout?: CheckoutRequest }, key?: string) =>
    request<Session | Operation>("/sessions", { method: "POST", headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) }, body: JSON.stringify(payload) }),
  repository: (id: string) => request<RepositoryInfo>(`/workspaces/${id}/repository`),
  worktrees: (id: string) => request<Worktree[]>(`/workspaces/${id}/worktrees`),
  worktree: (id: string) => request<Worktree>(`/worktrees/${id}`),
  worktreeAction: (id: string, action: "archive" | "restore" | "remove" | "cancel-launch") => request<Worktree>(`/worktrees/${id}/${action}`, { method: "POST" }),
  exportWorktree: (id: string, expected_tip: string) => request<Worktree>(`/worktrees/${id}/export`, { method: "POST", body: JSON.stringify({ expected_tip }) }),
  operation: (id: string) => request<Operation>(`/operations/${id}`),
  retryOperation: (id: string) => request<Operation>(`/operations/${id}/retry`, { method: "POST" }),
  deleteSession: (id: string) => request<void>(`/sessions/${id}`, { method: "DELETE" }),
  sessionLogs: (id: string) => request<SessionLog[]>(`/sessions/${id}/logs`),
  sessionLogTail: (id: string) => request<SessionLogTail>(`/sessions/${id}/logs/tail`),
  sessionConversation: (id: string) => request<ConversationMessage[]>(`/sessions/${id}/conversation`),
  sessionOutput: (id: string) => request<SessionOutput>(`/sessions/${id}/output`),
  actOnSession: (id: string, action: "suspend" | "resume" | "stop") =>
    request<Session>(`/sessions/${id}/actions`, { method: "POST", body: JSON.stringify({ action }) })
};

export async function waitForOperation(initial: Operation): Promise<void> {
  let operation = initial;
  const deadline = Date.now() + 10 * 60_000;
  while (operation.state === "pending" || operation.state === "running") {
    if (Date.now() > deadline) throw new Error("Preparation continues in the background. Check this task on the Workspaces page.");
    await new Promise((resolve) => window.setTimeout(resolve, 800));
    operation = await api.operation(operation.id);
  }
  if (operation.state !== "completed") throw new Error(operation.error ?? "Launch was cancelled. Check the branch task on the Workspaces page.");
}
