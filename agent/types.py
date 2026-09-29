"""Public request and result values shared by agent interfaces."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentRequest:
    user_message: str
    session_id: str | None = None
    attachments: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    active_skills: tuple[str, ...] = ()
    run_id: str | None = None


@dataclass(frozen=True)
class AgentResult:
    session_id: str | None
    final_text: str
    status: str
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
