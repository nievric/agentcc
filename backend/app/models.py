from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from typing import Literal

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_model_label(harness: str) -> str:
    """Return the user-facing label for a harness-native model choice."""
    if harness == "Claude Code":
        return "Default Claude model"
    if harness == "Codex":
        return "Default OpenAI model"
    return "Default harness model"


class SessionState(StrEnum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    IDLE = "idle"
    COMPLETED = "completed"


class AgentActivity(StrEnum):
    """Runtime activity within a lifecycle session state."""

    WORKING = "working"
    WAITING_FOR_INPUT = "waiting_for_input"
    NOT_STARTED = "not_started"
    PAUSED = "paused"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class ReasoningEffort(StrEnum):
    """Portable effort choices currently supported by the Codex adapter."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ModelProvider(StrEnum):
    """Only providers with reviewed adapters may be registered."""

    OPENROUTER = "OpenRouter"
    LOCAL_GATEWAY = "Local gateway"


class Session(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    workspace_id: UUID
    workspace: str
    workspace_folder_name: str
    harness: str = "Codex"
    model: str = "Default OpenAI model"
    model_id: UUID | None = None
    state: SessionState = SessionState.RUNNING
    agent_activity: AgentActivity = AgentActivity.UNKNOWN
    task: str
    tokens: int = 0
    cost_usd: float = 0
    cost_estimate_available: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    container_id: str | None = None
    volume_name: str | None = None
    harness_started: bool = False
    worktree_id: UUID | None = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None


class CheckoutRequest(BaseModel):
    mode: Literal["shared", "new_worktree", "existing_worktree"] = "shared"
    worktree_id: UUID | None = None
    source_branch: str | None = Field(default=None, min_length=1, max_length=200)
    expected_base_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    branch: str | None = Field(default=None, min_length=1, max_length=200)
    use_committed_version: bool = False
    reuse_git_identity: bool = False

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "existing_worktree" and self.worktree_id is None:
            raise ValueError("select an existing branch task")
        if self.mode != "existing_worktree" and self.worktree_id is not None:
            raise ValueError("worktree_id is only valid for an existing task")
        if self.mode == "new_worktree" and self.expected_base_commit is None:
            raise ValueError("preview the starting commit before launching")
        return self


class SessionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    workspace_id: UUID
    task: str = Field(default="", max_length=500)
    harness: str = "Codex"
    model: str = "Default OpenAI model"
    model_id: UUID | None = None
    checkout: CheckoutRequest = Field(default_factory=CheckoutRequest)


class SessionAction(BaseModel):
    action: str = Field(pattern="^(suspend|resume|stop)$")


class Telemetry(BaseModel):
    cpu_percent: int
    memory_used_gb: float
    memory_total_gb: int
    active_sessions: int
    session_tokens: int
    latency_ms: int


class Summary(BaseModel):
    total_sessions: int
    running: int
    suspended: int
    idle: int
    tokens_last_hour: int
    tokens_last_24_hours: int
    tokens_last_7_days: int
    estimated_cost_usd: float
    estimated_cost_available: bool
    uptime_label: str


class Workspace(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    description: str = ""
    kind: str = "Shared workspace"
    status: str
    attached_sessions: int
    last_opened: str
    volume_name: str
    folder_name: str
    host_path: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=300)


class WorkspaceUsage(BaseModel):
    workspace_id: UUID
    file_count: int | None = None
    storage_bytes: int | None = None
    available: bool = False


class WorkspaceDelete(BaseModel):
    """Deletion is explicit because durable workspace assets may be valuable."""

    mode: str = Field(pattern="^(soft|hard)$")
    discard_task_history: bool = False


class RepositoryInfo(BaseModel):
    available: bool = False
    reason: str | None = None
    head: str | None = None
    branch: str | None = None
    branches: dict[str, str] = Field(default_factory=dict)
    dirty: bool = False
    author_name: str | None = None
    author_email: str | None = None


class WorktreeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    source_branch: str | None = Field(default=None, min_length=1, max_length=200)
    expected_base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch: str | None = Field(default=None, min_length=1, max_length=200)
    use_committed_version: bool = False
    reuse_git_identity: bool = False


class Worktree(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    name: str
    branch: str
    source_branch: str | None = None
    base_commit: str
    state: Literal["creating", "ready", "archived", "removing", "removed", "needs_attention"] = "creating"
    host_path: str | None = None
    tip: str | None = None
    exported_commit: str | None = None
    dirty: bool | None = None
    error: str | None = None
    reserved_session_id: UUID | None = None
    operation_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)


class WorktreeExport(BaseModel):
    expected_tip: str = Field(pattern=r"^[0-9a-f]{40}$")


class Operation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    worktree_id: UUID
    state: Literal["pending", "running", "completed", "failed", "cancelled"] = "pending"
    session_id: UUID | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class WorkspaceStorageSettings(BaseModel):
    workspace_host_root: str | None = None
    available_workspace_host_roots: list[str] = Field(default_factory=list)


class WorkspaceStorageSettingsUpdate(BaseModel):
    workspace_host_root: str | None = None


class DisplaySettings(BaseModel):
    """Local display preferences; timestamps remain stored in UTC."""

    timezone: str | None = None


class DisplaySettingsUpdate(BaseModel):
    timezone: str | None = Field(default=None, max_length=100)


class SessionLog(BaseModel):
    id: int
    session_id: UUID
    stream: str
    text: str
    occurred_at: datetime


class SessionLogTail(BaseModel):
    text: str = ""
    available: bool = False
    truncated: bool = False


class ConversationMessage(BaseModel):
    role: str
    text: str
    occurred_at: datetime


class SessionOutput(BaseModel):
    """A bounded snapshot of the persistent harness terminal."""

    text: str = ""
    available: bool = False
    message: str | None = None
    agent_activity: AgentActivity = AgentActivity.UNKNOWN
    captured_at: datetime = Field(default_factory=utc_now)


class RegisteredModel(BaseModel):
    id: UUID
    provider: str
    display_name: str
    model_name: str
    endpoint: str
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    cached_input_cost_per_million: float | None = None
    credential_label: str
    api_key_last_four: str
    enabled: bool
    created_at: datetime


class ModelRegistration(BaseModel):
    provider: ModelProvider = ModelProvider.OPENROUTER
    display_name: str = Field(min_length=2, max_length=120)
    model_name: str = Field(min_length=2, max_length=200)
    endpoint: str = Field(min_length=8, max_length=500)
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM
    input_cost_per_million: float | None = Field(default=None, ge=0, le=1_000_000)
    output_cost_per_million: float | None = Field(default=None, ge=0, le=1_000_000)
    cached_input_cost_per_million: float | None = Field(default=None, ge=0, le=1_000_000)
    api_key: str | None = Field(default=None, max_length=1000)


class AppEvent(BaseModel):
    id: int
    type: str
    occurred_at: datetime = Field(default_factory=utc_now)
    data: dict[str, str | int | float]
