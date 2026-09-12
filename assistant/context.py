from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssistantContext:
    store: Any
    user_id: str
    conversation_id: str
