"""SQLite adapter over the existing conversation and summary tables."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class SessionStore:
    def __init__(self, store: Any) -> None:
        self.store = store

    def get_or_create(self, user_id: str, channel: str, thread_id: str):
        return self.store.get_or_create_conversation(user_id, channel, thread_id)

    def append(self, session_id: str, user_id: str, role: str, content: str,
               metadata: dict | None = None):
        if metadata and set(metadata) & {"role", "content"}:
            raise ValueError("message metadata cannot override role or content")
        return self.store.add_message_batch(
            session_id, user_id,
            [{**(metadata or {}), "role": role, "content": content}],
        )[0]

    def append_exchange(self, session_id: str, user_id: str,
                        assistant: dict, observations: list[dict]):
        return self.store.add_message_batch(session_id, user_id, [assistant, *observations])

    def recent_history(self, session_id: str, *, limit: int, max_chars: int) -> list[dict[str, str]]:
        return self.store.conversation_history(session_id, limit=limit, max_chars=max_chars)

    def compact(self, session_id: str, user_id: str, keep_recent: int, max_chars: int,
                reducer: Callable[[str, list[dict[str, str]]], str]) -> bool:
        """Advance summary and cursor together; never delete source messages."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT user_id FROM conversations WHERE id=?", (session_id,)
            ).fetchone()
            if owner is None or owner["user_id"] != user_id:
                raise ValueError("session does not belong to user")
            summary = db.execute(
                "SELECT summary, compacted_through_message_id FROM session_summaries "
                "WHERE conversation_id=? AND user_id=?", (session_id, user_id),
            ).fetchone()
            cursor = summary["compacted_through_message_id"] if summary else 0
            latest = db.execute(
                "SELECT id, content, metadata FROM messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?", (session_id, keep_recent),
            ).fetchall()
            if not latest:
                return False
            cutoff = latest[0]["id"] + 1
            used = 0
            for message in latest:
                cost = len(message["content"]) + (0 if message["metadata"] == "{}" else len(message["metadata"]))
                if used + cost > max_chars:
                    break
                cutoff = message["id"]
                used += cost
            rows = db.execute(
                "SELECT id, role, content FROM messages WHERE conversation_id=? "
                "AND id>? AND id<? ORDER BY id LIMIT 500",
                (session_id, cursor, cutoff),
            ).fetchall()
            if not rows:
                return False
            next_summary = reducer(
                summary["summary"] if summary else "",
                [{"role": row["role"], "content": row["content"]} for row in rows],
            )
            db.execute(
                "INSERT INTO session_summaries "
                "(conversation_id,user_id,summary,updated_at,compacted_through_message_id) "
                "VALUES (?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                "user_id=excluded.user_id, summary=excluded.summary, updated_at=excluded.updated_at, "
                "compacted_through_message_id=excluded.compacted_through_message_id",
                (session_id, user_id, next_summary, datetime.now(UTC).isoformat(), rows[-1]["id"]),
            )
            return True
