"""Identity-scoped session lifecycle and context preparation."""

from context import Compactor, ContextBudget, ContextManager

from .store import SessionStore


class SessionService:
    def __init__(self, store: SessionStore, budget: ContextBudget | None = None) -> None:
        self.store = store
        self.budget = budget or ContextBudget()
        self.context = ContextManager(self.budget)
        self.compactor = Compactor(self.budget)

    def resume(self, user_id: str, channel: str, thread_id: str):
        return self.store.get_or_create(user_id, channel, thread_id)

    def before_prompt(self, session_id: str, user_id: str) -> list[dict[str, str]]:
        while self.store.compact(
            session_id, user_id, self.budget.max_history_messages,
            self.budget.max_history_chars,
            self.compactor.compact,
        ):
            pass
        return self.context.history(self.store.recent_history(
            session_id,
            limit=self.budget.max_history_messages,
            max_chars=self.budget.max_history_chars,
        ))

    def append(self, session_id: str, user_id: str, role: str, content: str,
               metadata: dict | None = None):
        return self.store.append(session_id, user_id, role, content, metadata)
