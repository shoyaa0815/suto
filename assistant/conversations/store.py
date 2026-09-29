import json
from datetime import UTC, datetime
from uuid import uuid4

from workflows.storage.redaction import redact_text, redact_value

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

    @staticmethod
    def _validated_message(role: str, content: str, metadata: dict | None = None) -> tuple[str, str]:
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unsupported message role")
        content = redact_text(content).strip()
        safe_metadata = redact_value(metadata or {})
        if not isinstance(safe_metadata, dict):
            raise ValueError("message metadata must be an object")
        if role == "assistant" and set(safe_metadata) - {"tool_calls"}:
            raise ValueError("unsupported assistant message metadata")
        if role == "tool" and set(safe_metadata) - {"tool_call_id"}:
            raise ValueError("unsupported tool message metadata")
        if "tool_calls" in safe_metadata and (
            not isinstance(safe_metadata["tool_calls"], list)
            or not all(isinstance(call, dict) for call in safe_metadata["tool_calls"])
        ):
            raise ValueError("tool calls must be a list of objects")
        if "tool_call_id" in safe_metadata and (
            not isinstance(safe_metadata["tool_call_id"], str)
            or not safe_metadata["tool_call_id"]
        ):
            raise ValueError("tool call id must be a nonempty string")
        if role in {"system", "user"} and safe_metadata:
            raise ValueError("message metadata is unavailable for this role")
        encoded = json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True)
        may_be_empty = (
            (role == "assistant" and bool(safe_metadata.get("tool_calls")))
            or (role == "tool" and isinstance(safe_metadata.get("tool_call_id"), str))
        )
        if (not content and not may_be_empty) or len(content) + len(encoded) > 100_000:
            raise ValueError("message must contain 1-100000 characters")
        return content, encoded

    def add_message(self, conversation_id: str, role: str, content: str,
                    metadata: dict | None = None) -> Message:
        content, encoded = self._validated_message(role, content, metadata)
        now = _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at,metadata) "
                "VALUES (?,?,?,?,?)",
                (conversation_id, role, content, now, encoded),
            )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (now, conversation_id),
            )
            row = db.execute(
                "SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return self._to_message(row)

    def add_message_batch(self, conversation_id: str, user_id: str,
                          items: list[dict]) -> list[Message]:
        prepared = [
            (item["role"], *self._validated_message(
                item["role"], item.get("content") or "",
                {key: value for key, value in item.items() if key not in {"role", "content"}},
            ))
            for item in items
        ]
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT user_id FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if owner is None or owner["user_id"] != user_id:
                raise ValueError("session does not belong to user")
            created = []
            for role, content, metadata in prepared:
                cursor = db.execute(
                    "INSERT INTO messages(conversation_id,role,content,created_at,metadata) "
                    "VALUES (?,?,?,?,?)",
                    (conversation_id, role, content, now, metadata),
                )
                created.append(self._to_message(db.execute(
                    "SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)
                ).fetchone()))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        return created

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
            metadata_cost = 0 if message.metadata == "{}" else len(message.metadata)
            if used + len(message.content) + metadata_cost > max_chars:
                break
            item = {"role": message.role, "content": message.content}
            item.update(json.loads(message.metadata))
            selected.append(item)
            used += len(message.content) + metadata_cost
        return list(reversed(selected))

    def clear_conversation(self, conversation_id: str) -> int:
        """Delete the remembered messages for one conversation."""
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE conversation_id=?",
                (conversation_id,),
            )
            db.execute(
                "DELETE FROM session_summaries WHERE conversation_id=?",
                (conversation_id,),
            )
        return cursor.rowcount

    def reset_conversations(self) -> int:
        """Delete every conversation and its messages from the database."""
        with self._connect() as db:
            cursor = db.execute("DELETE FROM conversations")
        return cursor.rowcount

    def reset_user_conversations(self, user_id: str) -> int:
        """Delete every saved conversation owned by one user."""
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM conversations WHERE user_id=?",
                (user_id,),
            )
        return cursor.rowcount
