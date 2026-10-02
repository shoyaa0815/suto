"""Read-only, identity-scoped CLI chat history for the local web viewer."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


CONVERSATION_PAGE_SIZE = 50
MESSAGE_PAGE_SIZE = 100


@contextmanager
def _read_database(path: Path):
    database = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    database.row_factory = sqlite3.Row
    try:
        yield database
    finally:
        database.close()


def list_conversations(path: Path, offset: int = 0) -> dict:
    if not path.exists():
        return {"conversations": [], "next_offset": None}
    with _read_database(path) as database:
        rows = database.execute(
            "SELECT c.id, c.created_at, c.updated_at, "
            "(SELECT substr(m.content, 1, 120) FROM messages m "
            " WHERE m.conversation_id=c.id AND m.role='user' ORDER BY m.id LIMIT 1) AS preview "
            "FROM conversations c JOIN channel_identities i ON i.user_id=c.user_id "
            "AND i.channel='tui' AND i.external_id='local' "
            "WHERE c.channel='tui' "
            "ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?",
            (CONVERSATION_PAGE_SIZE + 1, offset),
        ).fetchall()
    has_more = len(rows) > CONVERSATION_PAGE_SIZE
    return {
        "conversations": [dict(row) for row in rows[:CONVERSATION_PAGE_SIZE]],
        "next_offset": offset + CONVERSATION_PAGE_SIZE if has_more else None,
    }


def list_messages(path: Path, conversation_id: str, after: int = 0) -> dict | None:
    if not path.exists():
        return None
    with _read_database(path) as database:
        conversation = database.execute(
            "SELECT c.id, c.created_at, c.updated_at FROM conversations c "
            "JOIN channel_identities i ON i.user_id=c.user_id "
            "AND i.channel='tui' AND i.external_id='local' "
            "WHERE c.id=? AND c.channel='tui'",
            (conversation_id,),
        ).fetchone()
        if conversation is None:
            return None
        rows = database.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id=? AND role IN ('user','assistant') AND id>? "
            "ORDER BY id LIMIT ?",
            (conversation_id, after, MESSAGE_PAGE_SIZE + 1),
        ).fetchall()
    has_more = len(rows) > MESSAGE_PAGE_SIZE
    messages = [dict(row) for row in rows[:MESSAGE_PAGE_SIZE]]
    return {
        "conversation": dict(conversation),
        "messages": messages,
        "next_after": messages[-1]["id"] if has_more else None,
    }
