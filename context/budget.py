"""Limits for stored conversation supplied to one model request."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBudget:
    max_history_messages: int = 20
    max_history_chars: int = 32_000
    max_summary_chars: int = 8_000
    max_retrieval_chars: int = 4_000
    max_retrieval_items: int = 5

    def __post_init__(self) -> None:
        if min(
            self.max_history_messages,
            self.max_history_chars,
            self.max_summary_chars,
            self.max_retrieval_chars,
            self.max_retrieval_items,
        ) < 1:
            raise ValueError("context budgets must be positive")
