"""Assemble model messages from a bounded view of persisted history."""

from .budget import ContextBudget


class ContextManager:
    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def history(self, items: list[dict[str, str]] | None) -> list[dict[str, str]]:
        selected: list[dict[str, str]] = []
        used = 0
        for item in reversed((items or [])[-self.budget.max_history_messages:]):
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
                continue
            if used + len(content) > self.budget.max_history_chars:
                break
            selected.append({"role": role, "content": content})
            used += len(content)
        return list(reversed(selected))

    def summary(self, content: str) -> str:
        return content[-self.budget.max_summary_chars:]

    def build(self, system_prompt: str, prompt: str, history: list[dict[str, str]] | None) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt},
            *self.history(history),
            {"role": "user", "content": prompt},
        ]
