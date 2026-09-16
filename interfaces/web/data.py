"""Bounded, read-only views of local assistant data; never migrate on a GET."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from workflows.storage.migrations import SCHEMA_VERSION
from workflows.storage.redaction import redact_text


LOCAL_OWNER = "SELECT user_id FROM channel_identities WHERE channel='tui' AND external_id='local'"


class DataUnavailable(Exception):
    pass


class DashboardData:
    def __init__(self, path: Path):
        self.path = path.absolute()

    @contextmanager
    def connect(self):
        if not self.path.exists():
            yield None
            return
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise DataUnavailable("Database schema needs a compatible Suto CLI version. No changes were made.")
                yield db
            finally:
                db.close()
        except sqlite3.Error as error:
            raise DataUnavailable("Database is unavailable. Try again after checking the CLI.") from error

    def sessions(self, query="", offset=0):
        with self.connect() as db:
            if db is None:
                return []
            rows = db.execute(f"""
                SELECT c.id,c.channel,c.updated_at,u.display_name,
                  (SELECT substr(content,1,100) FROM messages WHERE conversation_id=c.id
                   AND role='user' ORDER BY id LIMIT 1) AS title,
                  (SELECT count(*) FROM messages WHERE conversation_id=c.id) AS message_count
                FROM conversations c JOIN users u ON u.id=c.user_id
                WHERE c.user_id IN ({LOCAL_OWNER}) AND c.channel='tui'
                AND (instr(lower(c.id),lower(?))>0 OR EXISTS
                  (SELECT 1 FROM messages m WHERE m.conversation_id=c.id
                   AND instr(lower(m.content),lower(?))>0))
                ORDER BY c.updated_at DESC,c.id LIMIT 31 OFFSET ?
            """, (query, query, offset)).fetchall()
            return [dict(row) for row in rows]

    def messages(self, conversation_id, offset=0):
        with self.connect() as db:
            if db is None or not db.execute(f"""
                SELECT id FROM conversations WHERE id=? AND channel='tui'
                AND user_id IN ({LOCAL_OWNER})
            """, (conversation_id,)).fetchone():
                return None
            return [dict(row) for row in db.execute("""
                SELECT id,role,substr(content,1,16000) AS content,created_at,
                length(content)>16000 AS truncated FROM messages
                WHERE conversation_id=? ORDER BY id DESC LIMIT 31 OFFSET ?
            """, (conversation_id, offset))]

    def overview(self):
        result = {"database": "not_created", "sessions": 0, "tasks": 0, "reminders": 0,
                  "recent": [], "reminder_items": []}
        with self.connect() as db:
            if db is None:
                return result
            result["database"] = "available"
            for key, table, condition in (
                ("sessions", "conversations", "channel='tui'"),
                ("tasks", "assistant_tasks", "status='open'"),
                ("reminders", "reminders", "status='scheduled'"),
            ):
                result[key] = db.execute(
                    f"SELECT count(*) FROM {table} WHERE user_id IN ({LOCAL_OWNER}) AND {condition}"
                ).fetchone()[0]
            result["reminder_items"] = [dict(row) for row in db.execute(f"""
                SELECT title,remind_at,timezone FROM reminders
                WHERE user_id IN ({LOCAL_OWNER}) AND status='scheduled'
                ORDER BY remind_at LIMIT 5
            """)]
        result["recent"] = self.sessions()[:5]
        return result

    def logs(self, query="", event="", offset=0):
        with self.connect() as db:
            if db is None:
                return []
            return [{**dict(row), "detail": redact_text(row["detail"])} for row in db.execute("""
                SELECT id,job_id,event_type,substr(detail,1,4000) AS detail,created_at
                FROM structured_logs WHERE (?='' OR event_type=?)
                AND (instr(lower(detail),lower(?))>0 OR instr(lower(coalesce(job_id,'')),lower(?))>0)
                ORDER BY id DESC LIMIT 51 OFFSET ?
            """, (event, event, query, query, offset))]
