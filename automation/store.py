import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import ChangeEvent, Job, JobEvent, JobStatus, ToolEvent


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

    def create_job(
        self,
        prompt: str,
        mode: str = "agent",
        source: str = "cli",
        source_ref: str | None = None,
        workspace: str = ".",
        allow_write: bool = False,
    ) -> Job:
        job_id = f"job_{uuid4().hex[:8]}"
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, prompt, mode, status, source, source_ref,
                    workspace, allow_write, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                SET status = ?, started_at = ?, finished_at = NULL, error = NULL
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
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, finished_at = ?
                WHERE status = ?
                """,
                (
                    JobStatus.FAILED,
                    "worker stopped before job completed",
                    _now(),
                    JobStatus.RUNNING,
                ),
            )
        return cursor.rowcount
