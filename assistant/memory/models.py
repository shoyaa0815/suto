"""Data models for 3-tier memory system."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionSummary:
    conversation_id: str
    user_id: str
    summary: str
    updated_at: str


@dataclass(frozen=True)
class MemoryItem:
    id: str
    user_id: str
    category: str
    content: str
    created_at: str
    updated_at: str
