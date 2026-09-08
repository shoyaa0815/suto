import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .models import (
    ActionType,
    ApprovalEvent,
    ApprovalRequest,
    ApprovalStatus,
    ChangeEvent,
    CommandEvent,
    Job,
    JobEvent,
    JobStatus,
    JobStep,
    MissedRunPolicy,
    Schedule,
    ScheduleKind,
    StepStatus,
    ToolEvent,
    TriggerEvent,
    TriggerStatus,
)
from .redaction import redact_text, redact_value


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, path: str | Path = "data/suto.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_ref TEXT,
                    workspace TEXT NOT NULL DEFAULT '.',
                    allow_write INTEGER NOT NULL DEFAULT 0,
                    allow_command INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    error TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS jobs_status_created_idx
                    ON jobs(status, created_at);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS job_events_job_id_idx
                    ON job_events(job_id, id);

                CREATE TABLE IF NOT EXISTS tool_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    status TEXT NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    result_size INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS tool_events_job_id_idx
                    ON tool_events(job_id, id);

                CREATE TABLE IF NOT EXISTS change_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    before_sha256 TEXT,
                    after_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS change_events_job_id_idx
                    ON change_events(job_id, id);

                CREATE TABLE IF NOT EXISTS job_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
                    UNIQUE(job_id, position)
                );

                CREATE INDEX IF NOT EXISTS job_steps_job_id_idx
                    ON job_steps(job_id, position);

                CREATE TABLE IF NOT EXISTS command_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    stdout TEXT NOT NULL,
                    stderr TEXT NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS command_events_job_id_idx
                    ON command_events(job_id, id);

                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    action_summary TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    consumed_at TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS approval_requests_job_id_idx
                    ON approval_requests(job_id, requested_at);

                CREATE TABLE IF NOT EXISTS approval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    approval_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(approval_id) REFERENCES approval_requests(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS approval_events_job_id_idx
                    ON approval_events(job_id, id);

                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    allow_write INTEGER NOT NULL DEFAULT 0,
                    allow_command INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    missed_run_policy TEXT NOT NULL DEFAULT 'run_once',
                    retry_limit INTEGER NOT NULL DEFAULT 0,
                    retry_delay_seconds INTEGER NOT NULL DEFAULT 60,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS schedules_due_idx
                    ON schedules(enabled, next_run_at);

                CREATE TABLE IF NOT EXISTS trigger_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    job_id TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(schedule_id) REFERENCES schedules(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
                    UNIQUE(schedule_id, scheduled_for, attempt)
                );

                CREATE INDEX IF NOT EXISTS trigger_history_schedule_idx
                    ON trigger_history(schedule_id, id);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)")
            }
            if "workspace" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN workspace TEXT NOT NULL DEFAULT '.'"
                )
            if "allow_write" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN allow_write "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "allow_command" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN allow_command "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "attempt_count" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN attempt_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "retry_count" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN retry_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _to_job(row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        return Job(
            id=row["id"],
            prompt=row["prompt"],
            mode=row["mode"],
            status=JobStatus(row["status"]),
            source=row["source"],
            source_ref=row["source_ref"],
            workspace=row["workspace"],
            allow_write=bool(row["allow_write"]),
            allow_command=bool(row["allow_command"]),
            attempt_count=int(row["attempt_count"]),
            retry_count=int(row["retry_count"]),
            result=row["result"],
            error=row["error"],
            prompt_tokens=row["prompt_tokens"],
            output_tokens=row["output_tokens"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _to_event(row: sqlite3.Row | None) -> JobEvent | None:
        if row is None:
            return None
        return JobEvent(
            id=row["id"],
            job_id=row["job_id"],
            event_type=row["event_type"],
            detail=row["detail"],
            elapsed_seconds=row["elapsed_seconds"],
            total_tokens=row["total_tokens"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_tool_event(row: sqlite3.Row | None) -> ToolEvent | None:
        if row is None:
            return None
        return ToolEvent(
            id=row["id"],
            job_id=row["job_id"],
            tool_name=row["tool_name"],
            arguments=row["arguments"],
            status=row["status"],
            elapsed_seconds=row["elapsed_seconds"],
            result_size=row["result_size"],
            error=row["error"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_change_event(row: sqlite3.Row | None) -> ChangeEvent | None:
        if row is None:
            return None
        return ChangeEvent(
            id=row["id"],
            job_id=row["job_id"],
            path=row["path"],
            diff=row["diff"],
            before_sha256=row["before_sha256"],
            after_sha256=row["after_sha256"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_step(row: sqlite3.Row | None) -> JobStep | None:
        if row is None:
            return None
        return JobStep(
            id=row["id"],
            job_id=row["job_id"],
            position=row["position"],
            description=row["description"],
            status=StepStatus(row["status"]),
            result=row["result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_command_event(row: sqlite3.Row | None) -> CommandEvent | None:
        if row is None:
            return None
        return CommandEvent(
            id=row["id"],
            job_id=row["job_id"],
            command=row["command"],
            status=row["status"],
            exit_code=row["exit_code"],
            stdout=row["stdout"],
            stderr=row["stderr"],
            elapsed_seconds=row["elapsed_seconds"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_approval(row: sqlite3.Row | None) -> ApprovalRequest | None:
        if row is None:
            return None
        return ApprovalRequest(
            id=row["id"],
            job_id=row["job_id"],
            action_type=ActionType(row["action_type"]),
            action_digest=row["action_digest"],
            action_summary=row["action_summary"],
            preview=row["preview"],
            status=ApprovalStatus(row["status"]),
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            consumed_at=row["consumed_at"],
        )

    @staticmethod
    def _to_approval_event(row: sqlite3.Row | None) -> ApprovalEvent | None:
        if row is None:
            return None
        return ApprovalEvent(
            id=row["id"],
            approval_id=row["approval_id"],
            job_id=row["job_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            detail=row["detail"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_schedule(row: sqlite3.Row | None) -> Schedule | None:
        if row is None:
            return None
        return Schedule(
            id=row["id"],
            kind=ScheduleKind(row["kind"]),
            expression=row["expression"],
            timezone=row["timezone"],
            prompt=row["prompt"],
            workspace=row["workspace"],
            allow_write=bool(row["allow_write"]),
            allow_command=bool(row["allow_command"]),
            enabled=bool(row["enabled"]),
            missed_run_policy=MissedRunPolicy(row["missed_run_policy"]),
            retry_limit=int(row["retry_limit"]),
            retry_delay_seconds=int(row["retry_delay_seconds"]),
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_trigger(row: sqlite3.Row | None) -> TriggerEvent | None:
        if row is None:
            return None
        return TriggerEvent(
            id=int(row["id"]),
            schedule_id=row["schedule_id"],
            scheduled_for=row["scheduled_for"],
            idempotency_key=row["idempotency_key"],
            attempt=int(row["attempt"]),
            status=TriggerStatus(row["status"]),
            job_id=row["job_id"],
            detail=row["detail"],
            created_at=row["created_at"],
        )

    def create_job(
        self,
        prompt: str,
        mode: str = "agent",
        source: str = "cli",
        source_ref: str | None = None,
        workspace: str = ".",
        allow_write: bool = False,
        allow_command: bool = False,
    ) -> Job:
        job_id = f"job_{uuid4().hex[:8]}"
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, prompt, mode, status, source, source_ref,
                    workspace, allow_write, allow_command, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    redact_text(prompt),
                    mode,
                    JobStatus.QUEUED,
                    source,
                    source_ref,
                    workspace,
                    int(allow_write),
                    int(allow_command),
                    created_at,
                ),
            )
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError(f"failed to create job: {job_id}")
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._to_job(row)

    def list_jobs(self, limit: int = 20) -> list[Job]:
        safe_limit = min(max(int(limit), 1), 100)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [job for row in rows if (job := self._to_job(row)) is not None]

    def claim_next_job(self) -> Job | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            started_at = _now()
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = ?, finished_at = NULL, error = NULL,
                    attempt_count = attempt_count + 1
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.RUNNING,
                    started_at,
                    row["id"],
                    JobStatus.QUEUED,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        return self._to_job(claimed)

    def add_event(self, job_id: str, update: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, event_type, detail, elapsed_seconds,
                    total_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(update.get("activity", "progress")),
                    redact_text(update.get("detail", "")),
                    float(update.get("elapsed_seconds", 0)),
                    int(update.get("total_tokens", 0)),
                    _now(),
                ),
            )

    def list_events(self, job_id: str, limit: int = 50) -> list[JobEvent]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (job_id, safe_limit),
            ).fetchall()
        return [
            event for row in rows if (event := self._to_event(row)) is not None
        ]

    def latest_event(self, job_id: str) -> JobEvent | None:
        events = self.list_events(job_id, limit=1)
        return events[0] if events else None

    def add_tool_event(self, job_id: str, event: dict) -> None:
        arguments = json.dumps(
            redact_value(event.get("arguments") or {}),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_events (
                    job_id, tool_name, arguments, status, elapsed_seconds,
                    result_size, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(event.get("tool_name", "unknown")),
                    arguments,
                    str(event.get("status", "unknown")),
                    float(event.get("elapsed_seconds", 0)),
                    int(event.get("result_size", 0)),
                    redact_text(event["error"]) if event.get("error") else None,
                    _now(),
                ),
            )

    def list_tool_events(self, job_id: str, limit: int = 50) -> list[ToolEvent]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tool_events
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (job_id, safe_limit),
            ).fetchall()
        return [
            event
            for row in rows
            if (event := self._to_tool_event(row)) is not None
        ]

    def latest_tool_event(self, job_id: str) -> ToolEvent | None:
        events = self.list_tool_events(job_id, limit=1)
        return events[0] if events else None

    def tool_event_count(self, job_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM tool_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["count"])

    def add_change_event(self, job_id: str, event: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO change_events (
                    job_id, path, diff, before_sha256, after_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(event["path"]),
                    redact_text(event["diff"]),
                    event.get("before_sha256"),
                    str(event["after_sha256"]),
                    _now(),
                ),
            )

    def list_change_events(
        self,
        job_id: str,
        limit: int = 50,
    ) -> list[ChangeEvent]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM change_events
                WHERE job_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (job_id, safe_limit),
            ).fetchall()
        return [
            event
            for row in rows
            if (event := self._to_change_event(row)) is not None
        ]

    def changed_file_count(self, job_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT path) AS count "
                "FROM change_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["count"])

    def change_event_count(self, job_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM change_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["count"])

    def latest_changes_by_path(self, job_id: str) -> list[ChangeEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT current.* FROM change_events AS current
                INNER JOIN (
                    SELECT path, MAX(id) AS latest_id
                    FROM change_events WHERE job_id = ? GROUP BY path
                ) AS latest ON current.id = latest.latest_id
                ORDER BY current.path ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            event
            for row in rows
            if (event := self._to_change_event(row)) is not None
        ]

    def has_changed_path(self, job_id: str, path: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM change_events WHERE job_id = ? AND path = ? LIMIT 1",
                (job_id, path),
            ).fetchone()
        return row is not None

    def create_plan(self, job_id: str, descriptions: list[str]) -> list[JobStep]:
        """Create the first plan for a job without replacing an existing one."""
        return self._write_plan(job_id, descriptions, replace=False)

    def revise_plan(self, job_id: str, descriptions: list[str]) -> list[JobStep]:
        """Atomically replace the current plan while the job is running."""
        return self._write_plan(job_id, descriptions, replace=True)

    def _write_plan(
        self,
        job_id: str,
        descriptions: list[str],
        replace: bool,
    ) -> list[JobStep]:
        normalized = [redact_text(" ".join(str(item).split())) for item in descriptions]
        if not normalized or any(not item for item in normalized):
            raise ValueError("a plan requires at least one non-empty step")
        if len(normalized) > 20:
            raise ValueError("a plan cannot contain more than 20 steps")

        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError(f"job not found: {job_id}")
            if JobStatus(job["status"]) != JobStatus.RUNNING:
                raise ValueError("plans can be changed only while a job is running")

            existing = connection.execute(
                "SELECT 1 FROM job_steps WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
            if existing is not None and not replace:
                raise ValueError("a plan already exists; use revise_plan")
            if replace:
                connection.execute("DELETE FROM job_steps WHERE job_id = ?", (job_id,))

            connection.executemany(
                """
                INSERT INTO job_steps (
                    job_id, position, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        job_id,
                        position,
                        description,
                        StepStatus.PENDING,
                        timestamp,
                        timestamp,
                    )
                    for position, description in enumerate(normalized, start=1)
                ],
            )
        return self.list_steps(job_id)

    def list_steps(self, job_id: str) -> list[JobStep]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_steps WHERE job_id = ? ORDER BY position ASC",
                (job_id,),
            ).fetchall()
        return [step for row in rows if (step := self._to_step(row)) is not None]

    def update_step(
        self,
        job_id: str,
        position: int,
        status: str | StepStatus,
        result: str | None = None,
    ) -> JobStep:
        try:
            step_status = StepStatus(status)
        except ValueError as error:
            choices = ", ".join(item.value for item in StepStatus)
            raise ValueError(f"invalid step status; choose one of: {choices}") from error
        if position < 1:
            raise ValueError("step position must be at least 1")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError(f"job not found: {job_id}")
            if JobStatus(job["status"]) != JobStatus.RUNNING:
                raise ValueError("steps can be updated only while a job is running")
            if step_status == StepStatus.IN_PROGRESS:
                connection.execute(
                    """
                    UPDATE job_steps SET status = ?, updated_at = ?
                    WHERE job_id = ? AND status = ? AND position != ?
                    """,
                    (
                        StepStatus.PENDING,
                        _now(),
                        job_id,
                        StepStatus.IN_PROGRESS,
                        position,
                    ),
                )
            cursor = connection.execute(
                "SELECT status FROM job_steps WHERE job_id = ? AND position = ?",
                (job_id, position),
            ).fetchone()
            if cursor is None:
                raise ValueError(f"step not found: {position}")
            if (
                StepStatus(cursor["status"]) == StepStatus.COMPLETED
                and step_status != StepStatus.COMPLETED
            ):
                raise ValueError(
                    "a completed step cannot be reopened; revise the plan instead"
                )
            cursor = connection.execute(
                """
                UPDATE job_steps SET status = ?, result = ?, updated_at = ?
                WHERE job_id = ? AND position = ?
                """,
                (
                    step_status,
                    redact_text(result) if result is not None else None,
                    _now(),
                    job_id,
                    position,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"step not found: {position}")
            row = connection.execute(
                "SELECT * FROM job_steps WHERE job_id = ? AND position = ?",
                (job_id, position),
            ).fetchone()
        step = self._to_step(row)
        if step is None:
            raise RuntimeError(f"failed to update step: {position}")
        return step

    def current_step(self, job_id: str) -> JobStep | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM job_steps
                WHERE job_id = ? AND status IN (?, ?)
                ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, position ASC
                LIMIT 1
                """,
                (
                    job_id,
                    StepStatus.IN_PROGRESS,
                    StepStatus.PENDING,
                    StepStatus.IN_PROGRESS,
                ),
            ).fetchone()
        return self._to_step(row)

    def add_command_event(self, job_id: str, event: dict) -> None:
        command = json.dumps(
            redact_value(event.get("command") or []),
            ensure_ascii=False,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO command_events (
                    job_id, command, status, exit_code, stdout, stderr,
                    elapsed_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    command,
                    str(event.get("status", "unknown")),
                    event.get("exit_code"),
                    redact_text(event.get("stdout", "")),
                    redact_text(event.get("stderr", "")),
                    float(event.get("elapsed_seconds", 0)),
                    _now(),
                ),
            )

    def list_command_events(
        self,
        job_id: str,
        limit: int = 20,
    ) -> list[CommandEvent]:
        safe_limit = min(max(int(limit), 1), 100)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM command_events
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (job_id, safe_limit),
            ).fetchall()
        return [
            event
            for row in rows
            if (event := self._to_command_event(row)) is not None
        ]

    def latest_command_event(self, job_id: str) -> CommandEvent | None:
        events = self.list_command_events(job_id, limit=1)
        return events[0] if events else None

    def command_event_count(self, job_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM command_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["count"])

    @staticmethod
    def action_digest(action_type: str | ActionType, action: dict) -> str:
        kind = ActionType(action_type)
        canonical = json.dumps(
            {"action_type": kind.value, "action": action},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _add_approval_event(
        connection: sqlite3.Connection,
        approval_id: str,
        job_id: str,
        event_type: str,
        actor: str,
        detail: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO approval_events (
                approval_id, job_id, event_type, actor, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                job_id,
                event_type,
                redact_text(actor),
                redact_text(detail),
                _now(),
            ),
        )

    def request_or_consume_approval(
        self,
        job_id: str,
        action_type: str | ActionType,
        action: dict,
        summary: str,
        preview: str,
        ttl_seconds: int = 600,
    ) -> tuple[bool, ApprovalRequest]:
        """Consume an exact approval or persist a request and pause the job."""
        kind = ActionType(action_type)
        if kind == ActionType.READ:
            raise ValueError("read actions do not require approval")
        digest = self.action_digest(kind, action)
        timestamp = datetime.now(UTC)
        now = timestamp.isoformat()
        expires_at = (timestamp + timedelta(seconds=max(int(ttl_seconds), 1))).isoformat()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise ValueError(f"job not found: {job_id}")
            if JobStatus(job["status"]) != JobStatus.RUNNING:
                raise ValueError("approval can be requested only while a job is running")

            active_rows = connection.execute(
                """
                SELECT * FROM approval_requests
                WHERE job_id = ? AND status IN (?, ?)
                ORDER BY requested_at DESC
                """,
                (job_id, ApprovalStatus.PENDING, ApprovalStatus.APPROVED),
            ).fetchall()
            matching = None
            for row in active_rows:
                if datetime.fromisoformat(row["expires_at"]) <= timestamp:
                    connection.execute(
                        "UPDATE approval_requests SET status = ? WHERE id = ?",
                        (ApprovalStatus.EXPIRED, row["id"]),
                    )
                    self._add_approval_event(
                        connection,
                        row["id"],
                        job_id,
                        "expired",
                        "system",
                        "approval expired before action execution",
                    )
                    continue
                if row["action_digest"] == digest and matching is None:
                    matching = row
                    continue
                connection.execute(
                    "UPDATE approval_requests SET status = ? WHERE id = ?",
                    (ApprovalStatus.INVALIDATED, row["id"]),
                )
                self._add_approval_event(
                    connection,
                    row["id"],
                    job_id,
                    "invalidated",
                    "system",
                    "a different action was proposed",
                )

            if (
                matching is not None
                and ApprovalStatus(matching["status"]) == ApprovalStatus.APPROVED
            ):
                connection.execute(
                    """
                    UPDATE approval_requests
                    SET status = ?, consumed_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        ApprovalStatus.CONSUMED,
                        now,
                        matching["id"],
                        ApprovalStatus.APPROVED,
                    ),
                )
                self._add_approval_event(
                    connection,
                    matching["id"],
                    job_id,
                    "consumed",
                    "system",
                    "exact approved action authorized for one execution",
                )
                row = connection.execute(
                    "SELECT * FROM approval_requests WHERE id = ?",
                    (matching["id"],),
                ).fetchone()
                approval = self._to_approval(row)
                if approval is None:
                    raise RuntimeError("failed to consume approval")
                return True, approval

            if matching is None:
                approval_id = f"approval_{uuid4().hex[:10]}"
                connection.execute(
                    """
                    INSERT INTO approval_requests (
                        id, job_id, action_type, action_digest, action_summary,
                        preview, status, requested_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        job_id,
                        kind,
                        digest,
                        redact_text(summary),
                        redact_text(preview),
                        ApprovalStatus.PENDING,
                        now,
                        expires_at,
                    ),
                )
                self._add_approval_event(
                    connection,
                    approval_id,
                    job_id,
                    "requested",
                    "agent",
                    f"requested approval for {kind.value} action {digest}",
                )
            else:
                approval_id = matching["id"]

            connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = NULL, finished_at = NULL
                WHERE id = ? AND status = ?
                """,
                (JobStatus.WAITING_APPROVAL, job_id, JobStatus.RUNNING),
            )
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
        approval = self._to_approval(row)
        if approval is None:
            raise RuntimeError("failed to create approval request")
        return False, approval

    def list_approvals(self, job_id: str, limit: int = 50) -> list[ApprovalRequest]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approval_requests WHERE job_id = ?
                ORDER BY requested_at DESC LIMIT ?
                """,
                (job_id, safe_limit),
            ).fetchall()
        return [item for row in rows if (item := self._to_approval(row)) is not None]

    def latest_approval(self, job_id: str) -> ApprovalRequest | None:
        approvals = self.list_approvals(job_id, limit=1)
        return approvals[0] if approvals else None

    def list_approval_events(self, job_id: str) -> list[ApprovalEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM approval_events WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
        return [
            item for row in rows if (item := self._to_approval_event(row)) is not None
        ]

    def decide_approval(
        self,
        job_id: str,
        approve: bool,
        actor: str = "cli",
    ) -> tuple[bool, str]:
        timestamp = datetime.now(UTC)
        now = timestamp.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                return False, f"job not found: {job_id}"
            if JobStatus(job["status"]) != JobStatus.WAITING_APPROVAL:
                return False, "job is not waiting for approval"
            row = connection.execute(
                """
                SELECT * FROM approval_requests
                WHERE job_id = ? AND status = ?
                ORDER BY requested_at DESC LIMIT 1
                """,
                (job_id, ApprovalStatus.PENDING),
            ).fetchone()
            if row is None:
                return False, "no pending approval request"
            if datetime.fromisoformat(row["expires_at"]) <= timestamp:
                connection.execute(
                    "UPDATE approval_requests SET status = ? WHERE id = ?",
                    (ApprovalStatus.EXPIRED, row["id"]),
                )
                self._add_approval_event(
                    connection,
                    row["id"],
                    job_id,
                    "expired",
                    actor,
                    "approval decision arrived after expiry",
                )
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, started_at = NULL, finished_at = NULL
                    WHERE id = ? AND status = ?
                    """,
                    (JobStatus.QUEUED, job_id, JobStatus.WAITING_APPROVAL),
                )
                return False, "approval expired; job queued to request a new approval"

            decision = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            connection.execute(
                """
                UPDATE approval_requests
                SET status = ?, decided_at = ?, decided_by = ?
                WHERE id = ? AND status = ?
                """,
                (decision, now, redact_text(actor), row["id"], ApprovalStatus.PENDING),
            )
            self._add_approval_event(
                connection,
                row["id"],
                job_id,
                decision.value,
                actor,
                f"{decision.value} {row['action_type']} action {row['action_digest']}",
            )
            if approve:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error = NULL, started_at = NULL, finished_at = NULL
                    WHERE id = ? AND status = ?
                    """,
                    (JobStatus.QUEUED, job_id, JobStatus.WAITING_APPROVAL),
                )
                return True, f"approved {row['id']}; job queued"
            connection.execute(
                """
                UPDATE jobs SET status = ?, error = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.BLOCKED,
                    redact_text(f"approval rejected by {actor}: {row['action_summary']}"),
                    now,
                    job_id,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            return True, f"rejected {row['id']}; job blocked"

    def has_successful_verification_after_last_change(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM change_events "
                "WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if row is None:
            return True
        last_change_at = row["created_at"]
        for event in self.list_command_events(job_id, limit=100):
            if (
                event.created_at < last_change_at
                or event.status != "completed"
                or event.exit_code != 0
            ):
                continue
            try:
                command = json.loads(event.command)
            except (TypeError, json.JSONDecodeError):
                continue
            if not command:
                continue
            if command[0] in {"pytest", "ruff"}:
                return True
            if (
                command[0] in {"python", "python3"}
                and len(command) >= 3
                and command[1] == "-m"
                and command[2] in {"pytest", "compileall"}
            ):
                return True
        return False

    def complete_job(
        self,
        job_id: str,
        result: str,
        prompt_tokens: int,
        output_tokens: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, result = ?, error = NULL,
                    prompt_tokens = ?, output_tokens = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.COMPLETED,
                    redact_text(result),
                    prompt_tokens,
                    output_tokens,
                    _now(),
                    job_id,
                    JobStatus.RUNNING,
                ),
            )
        return cursor.rowcount == 1

    def fail_job(
        self,
        job_id: str,
        error: str,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, prompt_tokens = ?,
                    output_tokens = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.FAILED,
                    redact_text(error),
                    prompt_tokens,
                    output_tokens,
                    _now(),
                    job_id,
                    JobStatus.RUNNING,
                ),
            )
        return cursor.rowcount == 1

    def block_job(
        self,
        job_id: str,
        reason: str,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, prompt_tokens = ?,
                    output_tokens = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.BLOCKED,
                    redact_text(reason),
                    prompt_tokens,
                    output_tokens,
                    _now(),
                    job_id,
                    JobStatus.RUNNING,
                ),
            )
        return cursor.rowcount == 1

    def update_job_usage(
        self,
        job_id: str,
        prompt_tokens: int,
        output_tokens: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET prompt_tokens = ?, output_tokens = ?
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    prompt_tokens,
                    output_tokens,
                    job_id,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                    JobStatus.QUEUED,
                ),
            )

    def record_retry(
        self,
        job_id: str,
        reason: str,
        delay_seconds: float,
        prompt_tokens: int,
        output_tokens: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET retry_count = retry_count + 1,
                    prompt_tokens = ?, output_tokens = ?
                WHERE id = ? AND status = ?
                """,
                (prompt_tokens, output_tokens, job_id, JobStatus.RUNNING),
            )
        if cursor.rowcount == 1:
            self.add_event(
                job_id,
                {
                    "activity": "retry",
                    "detail": (
                        f"retrying after transient error in {delay_seconds:g}s: "
                        f"{redact_text(reason)}"
                    ),
                    "elapsed_seconds": 0,
                    "total_tokens": prompt_tokens + output_tokens,
                },
            )
            return True
        return False

    def interrupt_job(self, job_id: str, reason: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = ?, error = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.INTERRUPTED,
                    redact_text(reason),
                    _now(),
                    job_id,
                    JobStatus.RUNNING,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    UPDATE job_steps SET status = ?, updated_at = ?
                    WHERE job_id = ? AND status = ?
                    """,
                    (
                        StepStatus.PENDING,
                        _now(),
                        job_id,
                        StepStatus.IN_PROGRESS,
                    ),
                )
        return cursor.rowcount == 1

    def resume_job(self, job_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = NULL, started_at = NULL, finished_at = NULL
                WHERE id = ? AND status = ?
                """,
                (JobStatus.QUEUED, job_id, JobStatus.INTERRUPTED),
            )
        return cursor.rowcount == 1

    def cancel_job(self, job_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    JobStatus.CANCELLED,
                    _now(),
                    job_id,
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            if cursor.rowcount == 1:
                approvals = connection.execute(
                    """
                    SELECT id FROM approval_requests
                    WHERE job_id = ? AND status IN (?, ?)
                    """,
                    (job_id, ApprovalStatus.PENDING, ApprovalStatus.APPROVED),
                ).fetchall()
                for approval in approvals:
                    connection.execute(
                        "UPDATE approval_requests SET status = ? WHERE id = ?",
                        (ApprovalStatus.INVALIDATED, approval["id"]),
                    )
                    self._add_approval_event(
                        connection,
                        approval["id"],
                        job_id,
                        "invalidated",
                        "system",
                        "job was cancelled",
                    )
        return cursor.rowcount == 1

    def recover_interrupted_jobs(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE status = ?",
                    (JobStatus.RUNNING,),
                ).fetchall()
            ]
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, finished_at = ?
                WHERE status = ?
                """,
                (
                    JobStatus.INTERRUPTED,
                    "worker stopped before job completed; safe resume is available",
                    _now(),
                    JobStatus.RUNNING,
                ),
            )
            if running_ids:
                placeholders = ",".join("?" for _ in running_ids)
                connection.execute(
                    f"""
                    UPDATE job_steps SET status = ?, updated_at = ?
                    WHERE job_id IN ({placeholders}) AND status = ?
                    """,
                    (
                        StepStatus.PENDING,
                        _now(),
                        *running_ids,
                        StepStatus.IN_PROGRESS,
                    ),
                )
        return cursor.rowcount

    def create_schedule(
        self,
        *,
        kind: str | ScheduleKind,
        expression: str,
        timezone: str,
        prompt: str,
        workspace: str = ".",
        allow_write: bool = False,
        allow_command: bool = False,
        missed_run_policy: str | MissedRunPolicy = MissedRunPolicy.RUN_ONCE,
        retry_limit: int = 0,
        retry_delay_seconds: int = 60,
        next_run_at: str,
    ) -> Schedule:
        schedule_id = f"sch_{uuid4().hex[:8]}"
        timestamp = _now()
        schedule_kind = ScheduleKind(kind)
        missed_policy = MissedRunPolicy(missed_run_policy)
        if retry_limit < 0 or retry_limit > 10:
            raise ValueError("retry limit must be between 0 and 10")
        if retry_delay_seconds < 1 or retry_delay_seconds > 86_400:
            raise ValueError("retry delay must be between 1 and 86400 seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO schedules (
                    id, kind, expression, timezone, prompt, workspace,
                    allow_write, allow_command, enabled, missed_run_policy,
                    retry_limit, retry_delay_seconds, next_run_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    schedule_kind,
                    expression,
                    timezone,
                    redact_text(prompt),
                    workspace,
                    int(allow_write),
                    int(allow_command),
                    missed_policy,
                    retry_limit,
                    retry_delay_seconds,
                    next_run_at,
                    timestamp,
                    timestamp,
                ),
            )
        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            raise RuntimeError(f"failed to create schedule: {schedule_id}")
        return schedule

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._to_schedule(row)

    def list_schedules(self, limit: int = 100) -> list[Schedule]:
        safe_limit = min(max(int(limit), 1), 500)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [
            schedule
            for row in rows
            if (schedule := self._to_schedule(row)) is not None
        ]

    def list_due_schedules(self, now: str) -> list[Schedule]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM schedules
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now,),
            ).fetchall()
        return [
            schedule
            for row in rows
            if (schedule := self._to_schedule(row)) is not None
        ]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE schedules SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), _now(), schedule_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _insert_scheduled_job(
        connection: sqlite3.Connection,
        schedule: sqlite3.Row,
    ) -> str:
        job_id = f"job_{uuid4().hex[:8]}"
        schedule_id = (
            schedule["schedule_row_id"]
            if "schedule_row_id" in schedule.keys()
            else schedule["id"]
        )
        connection.execute(
            """
            INSERT INTO jobs (
                id, prompt, mode, status, source, source_ref, workspace,
                allow_write, allow_command, created_at
            ) VALUES (?, ?, 'agent', ?, 'schedule', ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                schedule["prompt"],
                JobStatus.QUEUED,
                schedule_id,
                schedule["workspace"],
                schedule["allow_write"],
                schedule["allow_command"],
                _now(),
            ),
        )
        return job_id

    def fire_schedule(
        self,
        schedule_id: str,
        scheduled_for: str,
        next_run_at: str | None,
        *,
        skip_detail: str | None = None,
    ) -> TriggerEvent | None:
        """Atomically materialize one occurrence and advance its schedule."""
        key_source = f"{schedule_id}:{scheduled_for}:1"
        idempotency_key = hashlib.sha256(key_source.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            schedule = connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if (
                schedule is None
                or not schedule["enabled"]
                or schedule["next_run_at"] != scheduled_for
            ):
                return None

            existing = connection.execute(
                "SELECT * FROM trigger_history WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._to_trigger(existing)

            detail = skip_detail or "created scheduled job"
            status = TriggerStatus.SKIPPED if skip_detail else TriggerStatus.CREATED
            job_id = None
            if not skip_detail:
                active = connection.execute(
                    """
                    SELECT 1 FROM trigger_history AS trigger
                    JOIN jobs ON jobs.id = trigger.job_id
                    WHERE trigger.schedule_id = ?
                      AND jobs.status IN (?, ?, ?, ?)
                    LIMIT 1
                    """,
                    (
                        schedule_id,
                        JobStatus.QUEUED,
                        JobStatus.RUNNING,
                        JobStatus.WAITING_APPROVAL,
                        JobStatus.INTERRUPTED,
                    ),
                ).fetchone()
                if active is not None:
                    status = TriggerStatus.SKIPPED
                    detail = "skipped because a previous run is still active"
                else:
                    job_id = self._insert_scheduled_job(connection, schedule)

            connection.execute(
                """
                INSERT INTO trigger_history (
                    schedule_id, scheduled_for, idempotency_key, attempt,
                    status, job_id, detail, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    scheduled_for,
                    idempotency_key,
                    status,
                    job_id,
                    detail,
                    _now(),
                ),
            )
            connection.execute(
                """
                UPDATE schedules
                SET next_run_at = ?, last_run_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_run_at, scheduled_for, _now(), schedule_id),
            )
            row = connection.execute(
                "SELECT * FROM trigger_history WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._to_trigger(row)

    def list_trigger_history(
        self, schedule_id: str, limit: int = 50
    ) -> list[TriggerEvent]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM trigger_history WHERE schedule_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (schedule_id, safe_limit),
            ).fetchall()
        return [
            trigger
            for row in rows
            if (trigger := self._to_trigger(row)) is not None
        ]

    def list_retryable_triggers(self, now: str) -> list[TriggerEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trigger.* FROM trigger_history AS trigger
                JOIN schedules ON schedules.id = trigger.schedule_id
                JOIN jobs ON jobs.id = trigger.job_id
                WHERE schedules.enabled = 1
                  AND trigger.status = ?
                  AND jobs.status = ?
                  AND trigger.attempt <= schedules.retry_limit
                  AND datetime(
                      jobs.finished_at,
                      '+' || schedules.retry_delay_seconds || ' seconds'
                  )
                      <= datetime(?)
                  AND NOT EXISTS (
                      SELECT 1 FROM trigger_history AS newer
                      WHERE newer.schedule_id = trigger.schedule_id
                        AND newer.scheduled_for = trigger.scheduled_for
                        AND newer.attempt = trigger.attempt + 1
                  )
                ORDER BY jobs.finished_at ASC
                """,
                (TriggerStatus.CREATED, JobStatus.FAILED, now),
            ).fetchall()
        return [
            trigger
            for row in rows
            if (trigger := self._to_trigger(row)) is not None
        ]

    def retry_trigger(self, trigger_id: int) -> TriggerEvent | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT trigger.*, jobs.status AS job_status,
                       schedules.enabled, schedules.retry_limit,
                       schedules.prompt, schedules.workspace,
                       schedules.allow_write, schedules.allow_command,
                       schedules.id AS schedule_row_id
                FROM trigger_history AS trigger
                JOIN jobs ON jobs.id = trigger.job_id
                JOIN schedules ON schedules.id = trigger.schedule_id
                WHERE trigger.id = ?
                """,
                (trigger_id,),
            ).fetchone()
            if (
                previous is None
                or not previous["enabled"]
                or previous["job_status"] != JobStatus.FAILED
                or previous["attempt"] > previous["retry_limit"]
            ):
                return None
            attempt = int(previous["attempt"]) + 1
            key_source = (
                f"{previous['schedule_id']}:{previous['scheduled_for']}:{attempt}"
            )
            key = hashlib.sha256(key_source.encode()).hexdigest()
            existing = connection.execute(
                "SELECT * FROM trigger_history WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                return self._to_trigger(existing)
            active = connection.execute(
                """
                SELECT 1 FROM trigger_history AS trigger
                JOIN jobs ON jobs.id = trigger.job_id
                WHERE trigger.schedule_id = ? AND jobs.status IN (?, ?, ?, ?)
                LIMIT 1
                """,
                (
                    previous["schedule_id"],
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                    JobStatus.INTERRUPTED,
                ),
            ).fetchone()
            if active is not None:
                return None
            job_id = self._insert_scheduled_job(connection, previous)
            connection.execute(
                """
                INSERT INTO trigger_history (
                    schedule_id, scheduled_for, idempotency_key, attempt,
                    status, job_id, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    previous["schedule_id"],
                    previous["scheduled_for"],
                    key,
                    attempt,
                    TriggerStatus.CREATED,
                    job_id,
                    f"created retry attempt {attempt}",
                    _now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM trigger_history WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._to_trigger(row)
