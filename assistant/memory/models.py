"""Legacy personal-memory model and shared summary import compatibility."""

from dataclasses import dataclass

# Preserve the legacy model import and class identity.
from assistant.conversations.models import SessionSummary


@dataclass(frozen=True)
class MemoryItem:
    id: str
    user_id: str
    category: str
    content: str
    created_at: str
    updated_at: str
