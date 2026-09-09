from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    WAITING_APPROVAL = "waiting_approval"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class ActionType(StrEnum):
    READ = "read"
    WRITE = "write"
    COMMAND = "command"
    DESTRUCTIVE = "destructive"


class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ScheduleKind(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class MissedRunPolicy(StrEnum):
    RUN_ONCE = "run_once"
    SKIP = "skip"


class TriggerStatus(StrEnum):
    CREATED = "created"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Automation:
    id: str
    name: str
    current_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AutomationVersion:
    id: str
    automation_id: str
    version: int
    description: str
    prompt_template: str
    parameter_schema: dict
    workspace: str
    allow_write: bool
    allow_command: bool
    created_at: str


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    current_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SkillVersion:
    id: str
    skill_id: str
    version: int
    instructions: str
    created_at: str


@dataclass(frozen=True)
class Job:
    id: str
    prompt: str
    mode: str
    status: JobStatus
    source: str
    source_ref: str | None
    workspace: str
    allow_write: bool
    allow_command: bool
    attempt_count: int
    retry_count: int
    result: str | None
    error: str | None
    prompt_tokens: int
    output_tokens: int
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


@dataclass(frozen=True)
class JobEvent:
    id: int
    job_id: str
    event_type: str
    detail: str
    elapsed_seconds: float
    total_tokens: int
    created_at: str


@dataclass(frozen=True)
class ToolEvent:
    id: int
    job_id: str
    tool_name: str
    arguments: str
    status: str
    elapsed_seconds: float
    result_size: int
    error: str | None
    created_at: str


@dataclass(frozen=True)
class ChangeEvent:
    id: int
    job_id: str
    path: str
    diff: str
    before_sha256: str | None
    after_sha256: str
    created_at: str


@dataclass(frozen=True)
class JobStep:
    id: int
    job_id: str
    position: int
    description: str
    status: StepStatus
    result: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CommandEvent:
    id: int
    job_id: str
    command: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float
    created_at: str


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    job_id: str
    action_type: ActionType
    action_digest: str
    action_summary: str
    preview: str
    status: ApprovalStatus
    requested_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str | None
    consumed_at: str | None


@dataclass(frozen=True)
class ApprovalEvent:
    id: int
    approval_id: str
    job_id: str
    event_type: str
    actor: str
    detail: str
    created_at: str


@dataclass(frozen=True)
class Schedule:
    id: str
    kind: ScheduleKind
    expression: str
    timezone: str
    prompt: str
    workspace: str
    allow_write: bool
    allow_command: bool
    enabled: bool
    missed_run_policy: MissedRunPolicy
    retry_limit: int
    retry_delay_seconds: int
    next_run_at: str | None
    last_run_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TriggerEvent:
    id: int
    schedule_id: str
    scheduled_for: str
    idempotency_key: str
    attempt: int
    status: TriggerStatus
    job_id: str | None
    detail: str
    created_at: str
