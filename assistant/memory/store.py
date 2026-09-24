"""SQLite and FTS5 storage mixin for 3-tier memory."""

import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from workflows.storage.redaction import redact_text

from .models import MemoryItem, SessionSummary


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryStore:
    """Store mixin providing session summaries and long-term memory via SQLite & FTS5."""

    @staticmethod
    def _to_session_summary(row) -> SessionSummary | None:
        return SessionSummary(**dict(row)) if row is not None else None

    @staticmethod
    def _to_memory_item(row) -> MemoryItem | None:
        return MemoryItem(**dict(row)) if row is not None else None

    # Tier 2: Session Memory (Working Summary)
    def save_session_summary(
        self,
        conversation_id: str,
        user_id: str,
        summary: str,
    ) -> SessionSummary:
        summary = redact_text(summary).strip()
        now = _now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO session_summaries(conversation_id, user_id, summary, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, user_id, summary, now),
            )
            row = db.execute(
                "SELECT * FROM session_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return self._to_session_summary(row)

    def get_session_summary(self, conversation_id: str) -> SessionSummary | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM session_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return self._to_session_summary(row)

    def delete_session_summary(self, conversation_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM session_summaries WHERE conversation_id = ?",
                (conversation_id,),
            )
        return cursor.rowcount > 0

    # Tier 3: Long-term Memory (FTS5 Knowledge Store)
    def save_memory(
        self,
        user_id: str,
        content: str,
        category: str = "general",
    ) -> MemoryItem:
        content = redact_text(content).strip()
        if not content:
            raise ValueError("memory content must not be empty")
        category = category.strip().casefold() or "general"
        memory_id = f"mem_{uuid4().hex[:12]}"
        now = _now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO assistant_memories(id, user_id, category, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, user_id, category, content, now, now),
            )
            row = db.execute(
                "SELECT * FROM assistant_memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return self._to_memory_item(row)

    def search_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[MemoryItem]:
        clean_words = [w for w in re.sub(r"[^\w\s]", " ", query).split() if w]
        safe_limit = min(max(int(limit), 1), 50)
        with self._connect() as db:
            if clean_words:
                fts_query = " OR ".join(f'"{w}"' for w in clean_words)
                try:
                    rows = db.execute(
                        """
                        SELECT m.* FROM assistant_memories m
                        JOIN assistant_memories_fts f ON m.id = f.memory_id
                        WHERE f.user_id = ? AND assistant_memories_fts MATCH ?
                        ORDER BY bm25(assistant_memories_fts), m.updated_at DESC
                        LIMIT ?
                        """,
                        (user_id, fts_query, safe_limit),
                    ).fetchall()
                    if rows:
                        return [self._to_memory_item(r) for r in rows]
                except sqlite3.OperationalError:
                    pass

            # Fallback to recent memories for this user
            rows = db.execute(
                """
                SELECT * FROM assistant_memories
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
            return [self._to_memory_item(r) for r in rows]

    def list_memories(
        self,
        user_id: str,
        category: str | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        safe_limit = min(max(int(limit), 1), 100)
        with self._connect() as db:
            if category:
                rows = db.execute(
                    """
                    SELECT * FROM assistant_memories
                    WHERE user_id = ? AND category = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, category.strip().casefold(), safe_limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT * FROM assistant_memories
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, safe_limit),
                ).fetchall()
        return [self._to_memory_item(r) for r in rows]

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM assistant_memories WHERE user_id = ? AND id = ?",
                (user_id, memory_id),
            )
        return cursor.rowcount > 0

    def clear_user_memories(self, user_id: str) -> int:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM assistant_memories WHERE user_id = ?",
                (user_id,),
            )
            db.execute(
                "DELETE FROM session_summaries WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount
