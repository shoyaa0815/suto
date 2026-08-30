import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import Job, JobEvent, JobStatus


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
                """
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

    def create_job(
        self,
        prompt: str,
        mode: str = "agent",
        source: str = "cli",
        source_ref: str | None = None,
    ) -> Job:
        job_id = f"job_{uuid4().hex[:8]}"
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, prompt, mode, status, source, source_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    prompt,
                    mode,
                    JobStatus.QUEUED,
                    source,
                    source_ref,
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
