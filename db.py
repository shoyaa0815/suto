# NOTE: this module is SQLite-specific. To switch to another database
# (e.g. Postgres, MySQL):
#   - Replace sqlite3.connect() with your driver's connection (e.g.
#     psycopg2.connect() / asyncpg.connect()).
#   - Placeholders here are "?" (SQLite). Postgres uses "%s", not "?".
#   - AUTOINCREMENT is SQLite-only; Postgres uses SERIAL/GENERATED ALWAYS AS IDENTITY.
#   - sqlite3 is synchronous. A networked DB should use an async driver
#     (asyncpg, aiomysql) and these functions should become "async def",
#     since a blocking call here would stall the whole Discord event loop.
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "suto.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def save_message(channel_id: str, role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (channel_id, role, content) VALUES (?, ?, ?)",
        (channel_id, role, content),
    )
    conn.commit()
    conn.close()


def get_history(channel_id: str, limit: int = 20) -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE channel_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (channel_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def clear_history(channel_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()
