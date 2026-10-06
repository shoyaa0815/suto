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
from permissions import PermissionEngine, PermissionPolicy
from permissions.models import PermissionDecision
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

    async def request_approval(self, run_id: str, tool_call_id: str,
                               tool_name: str):
        return None

    async def on_child_started(self, run_id: str, session_id: str | None,
                               parent_run_id: str) -> None:
        pass

    async def on_child_finished(self, run_id: str, status: str, text: str,
                                usage: dict[str, int]) -> None:
        pass

    async def on_tool_finished(
        self, call: ToolCall, status: str, content: Any, error: str | None, elapsed_seconds: float
    ) -> None:
        pass

    async def on_final(self, text: str, completed_tools: set[str]) -> str | AgentResult:
        return text

    async def on_tool_exchange(self, assistant: dict[str, Any], observations: list[dict[str, Any]]) -> None:
        pass


class AgentRuntime:
    def __init__(
        self,
        model: Model,
        tools: ToolRegistry,
        *,
        limits: ExecutionLimits | None = None,
        hooks: RuntimeHooks | None = None,
        permissions: PermissionEngine | None = None,
        passthrough_exceptions: tuple[type[Exception], ...] = (),
    ) -> None:
        self.model = model
        self.tools = tools
        self.tool_executor = ToolExecutor(tools)
        self.limits = limits or ExecutionLimits()
        self.hooks = hooks or RuntimeHooks()
        # Registration limits which tools exist; this policy governs requests
        # for those tools without coupling the runtime to concrete tool names.
        self.permissions = permissions if permissions is not None else PermissionEngine(PermissionPolicy())
        self.passthrough_exceptions = passthrough_exceptions
        self.state = AgentState()
        self.events: list[AgentEvent] = []
        self.run_id: str | None = None
        self.current_tool_call_id: str | None = None
        self.request: AgentRequest | None = None

    async def _emit(self, kind: str, run_id: str, session_id: str | None,
                    data: dict[str, Any] | None = None) -> None:
        metadata = self.request.metadata if self.request is not None else {}
        safe_data = dict(data or {})
        if metadata.get("child_role") in {"research", "coding", "review"}:
            safe_data["child_role"] = metadata["child_role"]
        event = AgentEvent(
            run_id, session_id, kind, safe_data,
            job_id=metadata.get("job_id"),
            parent_run_id=metadata.get("parent_run_id"),
            tool_call_id=self.current_tool_call_id,
        )
        self.events.append(event)
        await self.hooks.on_event(event)

    async def _transition(self, target: RunState, run_id: str, session_id: str | None) -> None:
        self.state.transition(target)
        await self._emit(f"agent.{target.value}", run_id, session_id)

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
        run_id = request.run_id or uuid4().hex
        self.run_id = run_id
        self.request = request
        self.current_tool_call_id = None
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
                await self._emit("model.requested", run_id, session_id, {"iteration": iteration})
                await self.hooks.on_model_requested(iteration)
                model_request = ModelRequest(
                    messages=messages,
                    available_tools=self.tools.export_model_schemas(),
                    generation_options=generation_options or {},
                )
                try:
                    response = await self._wait(
                        self.model.generate(model_request), self.limits.model_timeout_seconds
                    )
                except Exception as error:
                    await self._emit("model.failed", run_id, session_id, {"error_type": type(error).__name__})
                    raise
                usage["prompt_tokens"] += response.usage.prompt_tokens
                usage["output_tokens"] += response.usage.output_tokens
                await self._emit("model.completed", run_id, session_id, {
                    "iteration": iteration,
                    "prompt_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "tool_calls": len(response.tool_calls),
                })
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

                assistant_message = response.assistant_message or {
                    "role": "assistant", "content": response.text or "", "tool_calls": [
                        {"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}
                        for call in response.tool_calls
                    ],
                }
                messages.append(assistant_message)
                observations: list[dict[str, Any]] = []
                for call_index, call in enumerate(response.tool_calls, start=1):
                    # Trace IDs are internal so provider-supplied IDs never enter audit tables.
                    self.current_tool_call_id = f"{run_id}:{iteration}:{call_index}"
                    await self._transition(RunState.ACTION_REQUESTED, run_id, session_id)
                    await self._emit("tool.requested", run_id, session_id, {"iteration": iteration})
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
                        await self._emit("tool.failed", run_id, session_id, {"status": "unknown"})
                        reason = f"tool is not available for this request: {call.name}"
                        await self.hooks.on_tool_finished(call, "blocked", reason, reason, 0)
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    except ToolValidationError as error:
                        await self._emit("tool.failed", run_id, session_id, {"status": "invalid_arguments"})
                        reason = f"invalid tool arguments: {call.name}: {error}"
                        await self.hooks.on_tool_finished(call, "failed", reason, reason, 0)
                        return await self._stop("failed", f"tool failed: {call.name}", reason, run_id, session_id, usage,
                                                metadata={"error_code": "INVALID_INPUT"})
                    await self._emit("permission.requested", run_id, session_id)
                    try:
                        decision = self.permissions.decide(call.name)
                    except Exception:
                        decision = PermissionDecision(False, reason="permission decision failed")
                    if (
                        not isinstance(decision, PermissionDecision)
                        or type(decision.allowed) is not bool
                        or type(decision.requires_confirmation) is not bool
                        or not isinstance(decision.reason, str)
                        or (decision.allowed and decision.requires_confirmation)
                    ):
                        decision = PermissionDecision(False, reason="invalid permission decision")
                    if decision.requires_confirmation:
                        await self._emit("permission.approval_requested", run_id, session_id,
                                         {"tool_name": call.name})
                        try:
                            approval = await self.hooks.request_approval(
                                run_id, self.current_tool_call_id, call.name
                            )
                            decision = self.permissions.apply_approval(
                                call.name, *approval
                            ) if approval is not None else PermissionDecision(False, reason="approval unavailable")
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            decision = PermissionDecision(False, reason="approval unavailable")
                    authorized = False
                    if decision.allowed is True and decision.requires_confirmation is False:
                        try:
                            authorized = await self.hooks.authorize(call.name, deepcopy(prepared.arguments)) is True
                        except Exception:
                            authorized = False
                    if not authorized:
                        await self._emit("permission.denied", run_id, session_id)
                        reason = (
                            decision.reason if not decision.allowed else
                            f"tool is not allowed for this request: {call.name}"
                        )
                        await self.hooks.on_tool_finished(call, "blocked", reason, reason, 0)
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    granted = self.tool_executor.authorize(prepared, decision, authorized)
                    await self._emit("permission.allowed", run_id, session_id)
                    early_result = await self.hooks.on_tool_requested(call, iteration)
                    if early_result is not None:
                        target = RunState.WAITING_FOR_INPUT if early_result.status == "waiting_input" else RunState.FAILED
                        await self._transition(target, run_id, session_id)
                        return replace(early_result, session_id=session_id, usage=dict(usage))
                    reason = self.hooks.limit_reason(self.state.tool_calls, pending_tool=True)
                    if reason:
                        return await self._stop("blocked", reason, reason, run_id, session_id, usage)
                    await self._transition(RunState.EXECUTING, run_id, session_id)
                    await self._emit("tool.started", run_id, session_id, {"tool_name": call.name})
                    try:
                        execution = await self.tool_executor.execute(
                            granted,
                            timeout=self.limits.tool_timeout_seconds,
                            wait=self._wait,
                            passthrough_exceptions=self.passthrough_exceptions,
                        )
                    except self.passthrough_exceptions as error:
                        await self._emit(
                            "permission.approval_required" if hasattr(error, "approval_id") else "tool.failed",
                            run_id, session_id,
                            {} if hasattr(error, "approval_id") else {"status": "blocked"},
                        )
                        await self._transition(
                            RunState.WAITING_FOR_APPROVAL if hasattr(error, "approval_id") else RunState.FAILED,
                            run_id, session_id,
                        )
                        raise
                    result = execution.result
                    if not result.ok:
                        await self._emit("tool.failed", run_id, session_id, {"status": execution.status, "tool_name": call.name, "duration_ms": round(execution.elapsed_seconds * 1000)})
                        reason = result.error or f"tool failed: {call.name}"
                        await self.hooks.on_tool_finished(
                            call, execution.status, result.content, reason, execution.elapsed_seconds
                        )
                        if execution.status == "timed out":
                            return await self._stop("timed_out", reason, reason, run_id, session_id, usage)
                        return await self._stop(
                            "failed", f"tool failed: {call.name}",
                            execution.reported_error or reason, run_id, session_id, usage,
                            metadata={"error_code": execution.error_code}
                            if execution.error_code else None,
                        )
                    completed_tools.add(call.name)
                    await self._emit("tool.completed", run_id, session_id, {"tool_name": call.name, "duration_ms": round(execution.elapsed_seconds * 1000)})
                    await self.hooks.on_tool_finished(
                        call, "finished", result.content, None, execution.elapsed_seconds
                    )
                    await self._transition(RunState.OBSERVING, run_id, session_id)
                    observation = {"role": "tool", "content": str(result.content)}
                    if call.id:
                        observation["tool_call_id"] = call.id
                    messages.append(observation)
                    observations.append(observation)
                    await self._emit("tool.observed", run_id, session_id)
                await self.hooks.on_tool_exchange(assistant_message, observations)
                self.current_tool_call_id = None
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
                    session_id: str | None, usage: dict[str, int], *,
                    metadata: dict[str, Any] | None = None) -> AgentResult:
        if self.state.status != RunState.FAILED:
            await self._transition(RunState.FAILED, run_id, session_id)
        return AgentResult(session_id, text, status, dict(usage), metadata=metadata or {}, error=error)
