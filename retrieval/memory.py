"""Retrieve owner-scoped memories from the local SQLite FTS5 index."""

import sqlite3

from assistant.memory.service import PersistentMemory

from .base import RetrievalError, RetrievalResult


class MemoryRetriever:
    def __init__(self, memory: PersistentMemory) -> None:
        self.memory = memory

    def search_sync(self, query: str, limit: int = 5) -> list[RetrievalResult]:
        try:
            return [
                RetrievalResult("memory", item.id, item.content, item.category)
                for item in self.memory.search(query, limit)
            ]
        except sqlite3.DatabaseError as error:
            raise RetrievalError("memory retrieval unavailable") from error

    async def search(self, query: str, limit: int = 5) -> list[RetrievalResult]:
        return self.search_sync(query, limit)
