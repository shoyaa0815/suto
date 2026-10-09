"""Shared session-summary storage, independent of personal memory CRUD."""

from datetime import UTC, datetime

from workflows.storage.redaction import redact_text

from .models import SessionSummary


class SessionSummaryStore:
    @staticmethod
    def _to_session_summary(row) -> SessionSummary | None:
        return SessionSummary(**dict(row)) if row is not None else None

    def save_session_summary(
        self,
        conversation_id: str,
        user_id: str,
        summary: str,
    ) -> SessionSummary:
        summary = redact_text(summary).strip()
        now = datetime.now(UTC).isoformat()
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
