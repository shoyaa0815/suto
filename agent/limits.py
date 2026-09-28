"""Bounds that apply to a single agent run."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionLimits:
    max_iterations: int = 6
    max_tool_calls: int = 40
    model_timeout_seconds: float | None = None
    tool_timeout_seconds: float | None = None
    repeated_tool_call_limit: int | None = None

    def __post_init__(self) -> None:
        if self.max_iterations < 1 or self.max_tool_calls < 1:
            raise ValueError("iteration and tool-call limits must be positive")
        for seconds in (self.model_timeout_seconds, self.tool_timeout_seconds):
            if seconds is not None and seconds <= 0:
                raise ValueError("timeouts must be positive")
