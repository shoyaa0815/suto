"""Execute registered tools and normalize their outcomes for the agent loop."""

import asyncio
from copy import deepcopy
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .base import Tool
from .registry import ToolRegistry
from .types import ToolResult


class ToolNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class PreparedTool:
    tool: Tool
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolExecution:
    status: str
    result: ToolResult
    elapsed_seconds: float
    reported_error: str | None = None


async def _wait(awaitable: Awaitable[ToolResult], timeout: float | None) -> ToolResult:
    if timeout is not None:
        return await asyncio.wait_for(awaitable, timeout)
    return await awaitable


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def prepare(self, name: str, arguments: Any) -> PreparedTool:
        tool = self.registry.resolve(name)
        if tool is None:
            raise ToolNotFoundError(name)
        return PreparedTool(tool, deepcopy(self.registry.validate(tool, arguments)))

    async def execute(
        self,
        prepared: PreparedTool,
        *,
        timeout: float | None = None,
        wait: Callable[[Awaitable[ToolResult], float | None], Awaitable[ToolResult]] = _wait,
        passthrough_exceptions: tuple[type[Exception], ...] = (),
    ) -> ToolExecution:
        name = prepared.tool.name
        started = time.perf_counter()
        try:
            result = await wait(prepared.tool.execute(prepared.arguments), timeout)
            if not isinstance(result, ToolResult):
                raise TypeError(f"tool returned {type(result).__name__} instead of ToolResult")
        except passthrough_exceptions:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            reason = f"tool timed out: {name}"
            return ToolExecution(
                "timed out", ToolResult(False, reason, error=reason),
                time.perf_counter() - started,
            )
        except Exception as error:
            return ToolExecution(
                "failed",
                ToolResult(
                    False, f"tool failed: {name}",
                    error=type(error).__name__,
                ),
                time.perf_counter() - started,
                reported_error=f"tool failed: {name}",
            )
        return ToolExecution(
            "finished" if result.ok else "failed", result,
            time.perf_counter() - started,
        )
