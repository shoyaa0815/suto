"""User-scoped operations for explicitly saved persistent memory."""

from .models import MemoryItem
from .store import MemoryStore


class PersistentMemory:
    def __init__(self, store: MemoryStore, user_id: str) -> None:
        self.store = store
        self.user_id = user_id

    def save(self, content: str, category: str = "general") -> MemoryItem:
        return self.store.save_memory(self.user_id, content, category)

    def search(self, query: str, limit: int = 5) -> list[MemoryItem]:
        return self.store.search_memories(self.user_id, query, limit)

    def list(self, category: str | None = None, limit: int = 50) -> list[MemoryItem]:
        return self.store.list_memories(self.user_id, category, limit)

    def delete(self, memory_id: str) -> bool:
        return self.store.delete_memory(self.user_id, memory_id)
