from datetime import UTC, datetime
from uuid import uuid4

from automation.storage.redaction import redact_text

from .models import Conversation, Message


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ConversationStore:
    @staticmethod
    def _to_conversation(row) -> Conversation | None:
        return Conversation(**dict(row)) if row is not None else None

    @staticmethod
    def _to_message(row) -> Message | None:
        return Message(**dict(row)) if row is not None else None

    def get_or_create_conversation(
        self,
        user_id: str,
        channel: str,
        external_thread_id: str,
    ) -> Conversation:
        channel = channel.strip().casefold()
        external_thread_id = external_thread_id.strip()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE user_id=? AND channel=? "
                "AND external_thread_id=?",
                (user_id, channel, external_thread_id),
            ).fetchone()
            if row is None:
                now = _now()
                conversation_id = f"conv_{uuid4().hex[:12]}"
                db.execute(
                    "INSERT INTO conversations VALUES (?,?,?,?,?,?)",
                    (conversation_id, user_id, channel, external_thread_id, now, now),
                )
                row = db.execute(
                    "SELECT * FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
        return self._to_conversation(row)

    def add_message(self, conversation_id: str, role: str, content: str) -> Message:
        if role not in {"user", "assistant"}:
            raise ValueError("message role must be user or assistant")
        content = redact_text(content).strip()
        if not content or len(content) > 100_000:
            raise ValueError("message must contain 1-100000 characters")
        now = _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) "
                "VALUES (?,?,?,?)",
                (conversation_id, role, content, now),
            )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (now, conversation_id),
            )
            row = db.execute(
                "SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return self._to_message(row)

    def list_messages(self, conversation_id: str, limit: int = 20) -> list[Message]:
        safe_limit = min(max(int(limit), 1), 100)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, safe_limit),
            ).fetchall()
        return [self._to_message(row) for row in reversed(rows)]

    def conversation_history(
        self,
        conversation_id: str,
        *,
        limit: int = 20,
        max_chars: int = 32_000,
    ) -> list[dict[str, str]]:
        selected = []
        used = 0
        for message in reversed(self.list_messages(conversation_id, limit)):
            if used + len(message.content) > max_chars:
                break
            selected.append({"role": message.role, "content": message.content})
            used += len(message.content)
        return list(reversed(selected))

    def clear_conversation(self, conversation_id: str) -> int:
        """Delete the remembered messages for one conversation."""
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE conversation_id=?",
                (conversation_id,),
            )
        return cursor.rowcount

    def reset_conversations(self) -> int:
        """Delete every conversation and its messages from the database."""
        with self._connect() as db:
            cursor = db.execute("DELETE FROM conversations")
        return cursor.rowcount
