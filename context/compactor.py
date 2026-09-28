"""Bounded, deterministic excerpts of older conversation messages."""

from .budget import ContextBudget


class Compactor:
    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def compact(self, previous: str, messages: list[dict[str, str]]) -> str:
        entries = [previous] if previous else []
        entries.extend(
            f"{item['role']}: {item['content'].strip()}"
            for item in messages
            if item["content"].strip()
        )
        result = "\n".join(entries)
        limit = self.budget.max_summary_chars
        if len(result) > limit:
            marker = "[Earlier context omitted]\n"
            result = marker + result[-(limit - len(marker)):] if limit > len(marker) else result[-limit:]
        return result
