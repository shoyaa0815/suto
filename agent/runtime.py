"""Bounded agent loop over model, tool, permission and event ports."""

import asyncio
import json
from collections.abc import Awaitable
from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import uuid4

from llm.base import Model
from llm.types import ModelRequest, ModelResponse, ToolCall
from tools.executor import ToolExecutor, ToolNotFoundError
from tools.registry import ToolRegistry, ToolValidationError

from .events import AgentEvent
from .limits import ExecutionLimits
from .state import AgentState, RunState
from .types import AgentRequest, AgentResult


class RuntimeHooks:
    """Optional adapter callbacks; core behavior needs no interface imports."""

    async def wait(self, awaitable: Awaitable[Any]) -> Any:
        return await awaitable

    def limit_reason(self, tool_calls: int, *, pending_model=False, pending_tool=False) -> str | None:
        return None

    async def on_event(self, event: AgentEvent) -> None:
        pass

    async def on_model(self, response: ModelResponse, iteration: int) -> None:
        pass

    async def on_model_requested(self, iteration: int) -> None:
        pass

    async def on_tool_requested(self, call: ToolCall, iteration: int) -> AgentResult | None:
        return None

    async def authorize(self, name: str, args: dict[str, Any]) -> bool:
        return True

    async def on_tool_finished(
        self, call: ToolCall, status: str, content: Any, error: str | None, elapsed_seconds: float
    ) -> None:
        pass

    async def on_final(self, text: str, completed_tools: set[str]) -> str | AgentResult:
        return text


class AgentRuntime:
    def __init__(
        self,
        model: Model,
        tools: ToolRegistry,
        *,
        limits: ExecutionLimits | None = None,
        hooks: RuntimeHooks | None = None,
        passthrough_exceptions: tuple[type[Exception], ...] = (),
    ) -> None:
        self.model = model
        self.tools = tools
        self.tool_executor = ToolExecutor(tools)
        self.limits = limits or ExecutionLimits()
        self.hooks = hooks or RuntimeHooks()
        self.passthrough_exceptions = passthrough_exceptions
        self.state = AgentState()
        self.events: list[AgentEvent] = []

    async def _transition(self, target: RunState, run_id: str, session_id: str | None) -> None:
        self.state.transition(target)
        event = AgentEvent(run_id, session_id, f"agent.{target.value}")
        self.events.append(event)
        await self.hooks.on_event(event)

    async def _wait(self, awaitable: Awaitable[Any], timeout: float | None) -> Any:
        if timeout is not None:
            awaitable = asyncio.wait_for(awaitable, timeout=timeout)
        return await self.hooks.wait(awaitable)

    async def run(
        self,
        request: AgentRequest,
        messages: list[dict[str, Any]],
        *,
        generation_options: dict[str, Any] | None = None,
    ) -> AgentResult:
        self.state = AgentState()
        self.events = []
        run_id = uuid4().hex
        session_id = request.session_id
        usage = {"prompt_tokens": 0, "output_tokens": 0}
        tool_counts: dict[str, int] = {}
        completed_tools: set[str] = set()
        await self._transition(RunState.PREPARING, run_id, session_id)
        try:
            for iteration in range(1, self.limits.max_iterations + 1):
                self.state.iteration = iteration
                reason = self.hooks.limit_reason(self.state.tool_calls, pending_model=True)
                if reason:
                    return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                await self._transition(RunState.THINKING, run_id, session_id)
                await self.hooks.on_model_requested(iteration)
                model_request = ModelRequest(
                    messages=messages,
                    available_tools=self.tools.export_model_schemas(),
                    generation_options=generation_options or {},
                )
                response = await self._wait(
                    self.model.generate(model_request), self.limits.model_timeout_seconds
                )
                usage["prompt_tokens"] += response.usage.prompt_tokens
                usage["output_tokens"] += response.usage.output_tokens
                await self.hooks.on_model(response, iteration)
                reason = self.hooks.limit_reason(self.state.tool_calls)
                if reason:
                    return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                if not response.tool_calls:
                    answer = (response.text or "").strip()
                    if not answer:
                        return await self._stop(
                            "failed", "no response from ai", "AI returned an empty response",
                            run_id, session_id, usage,
                        )
                    finalized = await self.hooks.on_final(answer, completed_tools)
                    if isinstance(finalized, AgentResult):
                        target = {
                            "completed": RunState.COMPLETED,
                            "waiting_input": RunState.WAITING_FOR_INPUT,
                        }.get(finalized.status, RunState.FAILED)
                        await self._transition(target, run_id, session_id)
                        return replace(finalized, session_id=session_id, usage=dict(usage))
                    await self._transition(RunState.COMPLETED, run_id, session_id)
                    return AgentResult(session_id, finalized, "completed", usage)

                messages.append(response.assistant_message or {
                    "role": "assistant", "content": response.text or "", "tool_calls": [
                        {"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}
                        for call in response.tool_calls
                    ],
                })
                for call in response.tool_calls:
                    await self._transition(RunState.ACTION_REQUESTED, run_id, session_id)
                    self.state.tool_calls += 1
                    reason = self.hooks.limit_reason(self.state.tool_calls, pending_tool=True)
                    if reason:
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    if self.state.tool_calls > self.limits.max_tool_calls:
                        reason = f"tool-call limit exceeded ({self.limits.max_tool_calls} calls)"
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
                    tool_counts[signature] = tool_counts.get(signature, 0) + 1
                    if (self.limits.repeated_tool_call_limit is not None
                        and tool_counts[signature] >= self.limits.repeated_tool_call_limit):
                        reason = f"repeated identical tool call detected: {call.name} ({tool_counts[signature]} attempts)"
                        await self.hooks.on_tool_finished(call, "blocked", reason, reason, 0)
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    await self._transition(RunState.AUTHORIZING, run_id, session_id)
                    try:
                        prepared = self.tool_executor.prepare(call.name, call.arguments)
                    except ToolNotFoundError:
                        reason = f"tool is not available for this request: {call.name}"
                        await self.hooks.on_tool_finished(call, "blocked", reason, reason, 0)
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    except ToolValidationError as error:
                        reason = f"invalid tool arguments: {call.name}: {error}"
                        await self.hooks.on_tool_finished(call, "failed", reason, reason, 0)
                        return await self._stop("failed", f"tool failed: {call.name}", reason, run_id, session_id, usage)
                    if not await self.hooks.authorize(call.name, deepcopy(prepared.arguments)):
                        reason = f"tool is not allowed for this request: {call.name}"
                        await self.hooks.on_tool_finished(call, "blocked", reason, reason, 0)
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    early_result = await self.hooks.on_tool_requested(call, iteration)
                    if early_result is not None:
                        target = RunState.WAITING_FOR_INPUT if early_result.status == "waiting_input" else RunState.FAILED
                        await self._transition(target, run_id, session_id)
                        return replace(early_result, session_id=session_id, usage=dict(usage))
                    reason = self.hooks.limit_reason(self.state.tool_calls, pending_tool=True)
                    if reason:
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    await self._transition(RunState.EXECUTING, run_id, session_id)
                    try:
                        execution = await self.tool_executor.execute(
                            prepared,
                            timeout=self.limits.tool_timeout_seconds,
                            wait=self._wait,
                            passthrough_exceptions=self.passthrough_exceptions,
                        )
                    except self.passthrough_exceptions as error:
                        await self._transition(
                            RunState.WAITING_FOR_APPROVAL if hasattr(error, "approval_id") else RunState.FAILED,
                            run_id, session_id,
                        )
                        raise
                    result = execution.result
                    if not result.ok:
                        reason = result.error or f"tool failed: {call.name}"
                        await self.hooks.on_tool_finished(
                            call, execution.status, result.content, reason, execution.elapsed_seconds
                        )
                        if execution.status == "timed out":
                            return await self._stop("timed_out", reason, reason, run_id, session_id, usage)
                        return await self._stop(
                            "failed", f"tool failed: {call.name}",
                            execution.reported_error or reason, run_id, session_id, usage,
                        )
                    completed_tools.add(call.name)
                    await self.hooks.on_tool_finished(
                        call, "finished", result.content, None, execution.elapsed_seconds
                    )
                    await self._transition(RunState.OBSERVING, run_id, session_id)
                    observation = {"role": "tool", "content": str(result.content)}
                    if call.id:
                        observation["tool_call_id"] = call.id
                    messages.append(observation)
                # The next model call starts from the latest observation.
            reason = f"stopped after {self.limits.max_iterations} tool loops"
            return await self._stop("failed", "no response from ai", reason, run_id, session_id, usage)
        except asyncio.CancelledError:
            if self.state.status not in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                await self._transition(RunState.CANCELLED, run_id, session_id)
            raise
        except Exception:
            if self.state.status not in {RunState.COMPLETED, RunState.FAILED, RunState.WAITING_FOR_APPROVAL}:
                await self._transition(RunState.FAILED, run_id, session_id)
            raise

    async def _stop(self, status: str, text: str, error: str | None, run_id: str,
                    session_id: str | None, usage: dict[str, int]) -> AgentResult:
        if self.state.status != RunState.FAILED:
            await self._transition(RunState.FAILED, run_id, session_id)
        return AgentResult(session_id, text, status, dict(usage), error=error)
