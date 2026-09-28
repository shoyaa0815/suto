"""Small, read-only view of the existing local job database."""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def snapshot(path: str | Path | None = None) -> dict:
    database = Path(path or os.environ.get("SUTO_DB_PATH", "data/suto.db")).expanduser().absolute()
    unavailable = {"available": False, "error": False, "worker": "unknown", "jobs": {},
                   "recent_jobs": [], "skills": [], "skill_count": 0}
    if database.is_symlink() or not database.is_file():
        return unavailable
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            connection.row_factory = sqlite3.Row
            jobs = dict(connection.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"))
            recent = [dict(row) for row in connection.execute(
                "SELECT id, status, created_at FROM jobs ORDER BY created_at DESC LIMIT 8")]
            skills = [dict(row) for row in connection.execute(
                "SELECT name, current_version, updated_at FROM skills ORDER BY name LIMIT 100")]
            skill_count = connection.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            worker = connection.execute("SELECT state, heartbeat FROM worker_state WHERE id=1").fetchone()
    except (OSError, sqlite3.Error):
        return {**unavailable, "error": True}
    worker_state = "stopped"
    if worker:
        try:
            heartbeat = datetime.fromisoformat(worker["heartbeat"])
            if (worker["state"] == "running" and heartbeat.tzinfo is not None
                    and 0 <= (datetime.now(UTC) - heartbeat).total_seconds() < 30):
                worker_state = "running"
        except (TypeError, ValueError):
            pass
    return {"available": True, "worker": worker_state, "jobs": jobs,
            "recent_jobs": recent, "skills": skills, "skill_count": skill_count}
