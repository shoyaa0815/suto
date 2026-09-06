import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import (
    ChangeEvent,
    CommandEvent,
    Job,
    JobEvent,
    JobStatus,
    JobStep,
    StepStatus,
    ToolEvent,
)


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
                    prompt,
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
                    str(update.get("detail", "")),
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
            event.get("arguments") or {},
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
                    event.get("error"),
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
                    str(event["diff"]),
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
        normalized = [" ".join(str(item).split()) for item in descriptions]
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
                (step_status, result, _now(), job_id, position),
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
            event.get("command") or [],
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
                    str(event.get("stdout", "")),
                    str(event.get("stderr", "")),
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
                    result,
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
                    error,
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
                    reason,
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
                WHERE id = ? AND status = ?
                """,
                (prompt_tokens, output_tokens, job_id, JobStatus.RUNNING),
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
                        f"{reason}"
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
                    reason,
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
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    JobStatus.CANCELLED,
                    _now(),
                    job_id,
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                ),
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
