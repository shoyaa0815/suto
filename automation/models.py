from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
