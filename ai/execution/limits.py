"""Execution-budget checks shared by model and tool awaits."""

import asyncio
import inspect
import time
from typing import Any

from workflows.runtime.context import ExecutionContext, ExecutionLimitExceeded


class ExecutionGuard:
    def __init__(self, context: ExecutionContext | None, progress, started: float):
        self.context = context
        self.progress = progress
        self.started = started

    def limit_reason(
        self,
        tool_calls: int = 0,
        *,
        pending_tool: bool = False,
        pending_model: bool = False,
    ) -> str | None:
        if self.context is None:
            return None
        limits = self.context.limits
        if self.context.budget_check is not None:
            reason = self.context.budget_check(
                pending_tool=pending_tool,
                pending_model=pending_model,
            )
            if reason:
                return reason
        elapsed = time.perf_counter() - self.started
        if elapsed >= limits.max_elapsed_seconds:
            return (
                "job elapsed-time limit reached "
                f"({limits.max_elapsed_seconds:g} seconds)"
            )
        if self.progress.total_tokens > limits.max_tokens:
            return f"job token limit exceeded ({limits.max_tokens} tokens)"
        if tool_calls > limits.max_tool_calls:
            return f"job tool-call limit exceeded ({limits.max_tool_calls} calls)"
        return None

    async def wait(self, awaitable: Any):
        if self.context is None:
            return await awaitable
        limit = self.context.limits.max_elapsed_seconds
        remaining = limit - (time.perf_counter() - self.started)
        if self.context.budget_seconds is not None:
            remaining = min(remaining, self.context.budget_seconds())
        if remaining <= 0:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise ExecutionLimitExceeded(
                f"job elapsed-time limit reached ({limit:g} seconds)"
            )
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError as error:
            budget_expired = (
                self.context.budget_seconds is not None
                and self.context.budget_seconds() <= 0
            )
            if time.perf_counter() - self.started >= limit or budget_expired:
                raise ExecutionLimitExceeded(
                    f"job elapsed-time limit reached ({limit:g} seconds)"
                ) from error
            raise
