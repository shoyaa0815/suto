"""Normalized model request, response and tool-call values."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    available_tools: list[dict[str, Any]]
    generation_options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: ModelUsage = field(default_factory=ModelUsage)
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    assistant_message: dict[str, Any] | None = None
