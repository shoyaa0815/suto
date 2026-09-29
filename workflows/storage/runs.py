"""Persistent agent run ownership, terminal results, and session Skill names."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .redaction import redact_text


ACTIVE = frozenset({"queued", "running"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted", "blocked", "timed_out", "waiting_input"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _process_start(pid: int) -> str:
    try:
        # Linux field 22 identifies a process across PID reuse.
        return Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


OWNER_PID = os.getpid()
OWNER_START = _process_start(OWNER_PID)


def _owner_alive(pid: int, started: str) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not started or _process_start(pid) == started


class SessionBusyError(RuntimeError):
    pass


class RunStore:
    def recover_interrupted_runs(self, *, force: bool = False) -> int:
        """Mark orphaned active runs terminal. Force is for controlled recovery tests."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,owner_pid,owner_start FROM agent_runs WHERE status IN ('queued','running')"
            ).fetchall()
            stale = [row["id"] for row in rows if force or not _owner_alive(row["owner_pid"], row["owner_start"])]
            for run_id in stale:
                db.execute(
                    "UPDATE agent_runs SET status='interrupted', completed_at=?, "
                    "final_text='Request interrupted.', error='interrupted' WHERE id=?",
                    (_now(), run_id),
                )
            return len(stale)

    def begin_agent_run(self, session_id: str, *, run_id: str | None = None,
                        parent_run_id: str | None = None) -> str:
        run_id = run_id or uuid4().hex
        self.recover_interrupted_runs()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO agent_runs(id,session_id,parent_run_id,status,created_at,owner_pid,owner_start) "
                    "VALUES (?,?,?,'queued',?,?,?)",
                    (run_id, session_id, parent_run_id, _now(), OWNER_PID, OWNER_START),
                )
        except sqlite3.IntegrityError as error:
            if "agent_runs_active_session_idx" in str(error) or "agent_runs.session_id" in str(error):
                raise SessionBusyError("Session already has an active run") from error
            raise
        return run_id

    def start_agent_run(self, run_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE agent_runs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (_now(), run_id),
            )

    def finish_agent_run(self, run_id: str, status: str, *, final_text: str = "",
                         error: str | None = None, usage: dict | None = None) -> None:
        if status not in TERMINAL:
            raise ValueError("invalid terminal run status")
        safe_usage = {
            key: value for key, value in (usage or {}).items()
            if key in {"prompt_tokens", "output_tokens"} and type(value) is int and value >= 0
        }
        with self._connect() as db:
            db.execute(
                "UPDATE agent_runs SET status=?, completed_at=?, final_text=?, error=?, usage_json=? "
                "WHERE id=? AND status IN ('queued','running')",
                (status, _now(), redact_text(final_text),
                 redact_text(error) if error else None, json.dumps(safe_usage), run_id),
            )

    def get_agent_run(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["usage"] = json.loads(result.pop("usage_json"))
        result.pop("owner_pid")
        result.pop("owner_start")
        return result

    def get_session_skills(self, session_id: str) -> tuple[str, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT skill_name FROM session_skills WHERE session_id=? ORDER BY position",
                (session_id,),
            ).fetchall()
        return tuple(row["skill_name"] for row in rows)

    def set_session_skills(self, session_id: str, names: tuple[str, ...]) -> None:
        if len(names) != len(set(names)) or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("invalid Skill selection")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM session_skills WHERE session_id=?", (session_id,))
            db.executemany(
                "INSERT INTO session_skills(session_id,skill_name,position) VALUES (?,?,?)",
                [(session_id, name, index) for index, name in enumerate(names)],
            )
