"""Compatibility adapter from Suto's existing AI lifecycle to AgentRuntime."""

import asyncio
import json

from agent import AgentRequest, AgentResult, AgentRuntime, RuntimeHooks
from agent.limits import ExecutionLimits
from application.modes import CLARIFICATIONS_ENABLED
from assistant.tasks.tools import REMINDER_CREATION_TOOL_NAMES, reminder_creation_requested
from planning import Planner, Replanner
from permissions import PermissionEngine, PermissionPolicy
from sessions import SessionStore
from tools.clarification import parse_request as parse_clarification_request
from tools.registry import FunctionTool, ToolRegistry
from workflows.runtime.context import ApprovalRequired, ExecutionLimitExceeded

from .. import config, response
from ..providers.factory import build_model_router
from ..tooling.events import audit_tool_arguments, emit_tool_event, tool_detail


class _LegacyHooks(RuntimeHooks):
    def __init__(self, owner: "ModelToolLoop") -> None:
        self.owner = owner

    async def on_event(self, event):
        self.owner.progress.run_id = event.run_id
        context = self.owner.assistant_context
        store = context.store if context is not None else (
            self.owner.execution_context.plan_store if self.owner.execution_context is not None else None
        )
        if store is not None:
            store.add_run_event(event)

    async def wait(self, awaitable):
        return await self.owner.guard.wait(awaitable)

    def limit_reason(self, tool_calls, *, pending_model=False, pending_tool=False):
        return self.owner.guard.limit_reason(
            tool_calls, pending_model=pending_model, pending_tool=pending_tool
        )

    async def on_model_requested(self, iteration):
        await self.owner.progress.emit(
            "model", f"waiting for AI (loop {iteration}/{self.owner.max_rounds})", iteration
        )

    async def on_model(self, model_response, iteration):
        self.owner.progress.record_usage({
            "prompt_eval_count": model_response.usage.prompt_tokens,
            "eval_count": model_response.usage.output_tokens,
        })
        await self.owner.progress.emit()

    async def on_tool_requested(self, call, iteration):
        await self.owner.progress.emit("tool", tool_detail(call.name, call.arguments), iteration)
        if call.name != "ask_user" or not CLARIFICATIONS_ENABLED:
            return None
        try:
            clarification = parse_clarification_request(call.arguments)
        except ValueError as error:
            reason = f"failed: invalid clarification request: {error}"
            self.owner.outcome = reason
            return AgentResult(None, "no response from ai", "failed", error=reason)
        await self.on_tool_finished(call, "waiting_input", "", None, 0)
        self.owner.outcome = "waiting for user clarification"
        text = clarification["question"] + "\n" + "\n".join(
            f"- {option}" for option in clarification["options"]
        )
        return AgentResult(None, text, "waiting_input", metadata={"clarification": clarification})

    async def authorize(self, name, args):
        return name in self.owner.tools

    async def on_tool_finished(self, call, status, content, error, elapsed_seconds):
        arguments = (
            audit_tool_arguments(call.name, call.arguments)
            if isinstance(call.arguments, dict)
            else {"invalid_arguments_type": type(call.arguments).__name__}
        )
        await emit_tool_event(
            self.owner.tool_event_callback,
            {
                "job_id": self.owner.execution_context.job_id if self.owner.execution_context else None,
                "run_id": self.owner.runtime.run_id,
                "tool_call_id": self.owner.runtime.current_tool_call_id,
                "tool_name": call.name,
                "arguments": arguments,
                "status": status,
                "elapsed_seconds": elapsed_seconds,
                "result_size": len(str(content).encode("utf-8")),
                "error": error,
            },
        )
        if status == "finished":
            await self.owner.progress.emit(
                "tool_done", f"{tool_detail(call.name, call.arguments)}: finished"
            )

    async def on_tool_exchange(self, assistant, observations):
        context = self.owner.assistant_context
        if context is not None and context.conversation_id:
            SessionStore(context.store).append_exchange(
                context.conversation_id, context.user_id, assistant, observations
            )

    async def on_final(self, text, completed_tools):
        owner = self.owner
        if (
            owner.assistant_context is not None
            and reminder_creation_requested(owner.prompt)
            and not completed_tools & REMINDER_CREATION_TOOL_NAMES
        ):
            owner.outcome = "failed: reminder tool was not completed"
            message = (
                "สร้างการแจ้งเตือนไม่สำเร็จ กรุณาลองอีกครั้ง"
                if owner.reply_language.code == "th"
                else "I couldn't create the reminder. Please try again."
            )
            return AgentResult(None, message, "failed", error=owner.outcome)
        answer = await owner.guard.wait(
            response.enforce_reply_language(
                owner.session, text, owner.reply_language, owner.progress
            )
        )
        reason = owner.guard.limit_reason(owner.runtime.state.tool_calls)
        if reason:
            owner.outcome = f"blocked: {reason}"
            return AgentResult(None, reason, "blocked", error=reason)
        return response.to_ascii_digits(answer)


class ModelToolLoop:
    """Keep the established caller contract while the core loop moves to agent/."""

    def __init__(
        self, *, session, messages, tool_schemas, tools, max_rounds, prompt,
        mode, think, reply_language, assistant_context, execution_context,
        tool_event_callback, progress, guard, build_result,
        agent_request: AgentRequest,
    ) -> None:
        self.session = session
        self.messages = messages
        self.tool_schemas = tool_schemas
        self.tools = tools
        self.max_rounds = max_rounds
        self.prompt = prompt
        self.mode = mode
        self.think = think
        self.reply_language = reply_language
        self.assistant_context = assistant_context
        self.execution_context = execution_context
        self.tool_event_callback = tool_event_callback
        self.progress = progress
        self.guard = guard
        self.build_result = build_result
        self.agent_request = agent_request
        self.outcome = "completed"
        self.runtime = None
        self.plan = None

    async def run(self):
        registry = ToolRegistry()
        schemas = {item["function"]["name"]: item["function"] for item in self.tool_schemas}
        for name, handler in self.tools.items():
            schema = schemas[name]
            parameters = dict(schema["parameters"])
            parameters.setdefault("additionalProperties", False)
            registry.register(FunctionTool(
                name, schema.get("description", ""), parameters, handler
            ))
        job_limits = self.execution_context.limits if self.execution_context else None
        model = build_model_router(self.session)
        if self.execution_context is None and Planner.needs_plan(self.prompt):
            await self.progress.emit("planning", "drafting a plan")
            planner = Planner(model)
            try:
                planned_response = await self.guard.wait(asyncio.wait_for(
                    model.generate(planner.request(self.prompt)),
                    timeout=config.AI_TIMEOUT_SECONDS,
                ))
            except (asyncio.CancelledError, ApprovalRequired, ExecutionLimitExceeded):
                raise
            except Exception:
                # Planning is optional; a failed planning call must not block
                # the normal agent run or expose provider error details.
                planned_response = None
            if planned_response is not None:
                self.progress.record_usage({
                    "prompt_eval_count": planned_response.usage.prompt_tokens,
                    "eval_count": planned_response.usage.output_tokens,
                })
                try:
                    self.plan = planner.parse(self.prompt, planned_response)
                except ValueError:
                    # An invalid proposal also falls back to the normal run.
                    pass
            if self.plan is not None:
                self.messages.append({
                    "role": "assistant",
                    "content": "Proposed plan (guidance only; no action has been completed): "
                    + json.dumps(
                        [step.description for step in self.plan.steps],
                        ensure_ascii=False,
                    ),
                })
        self.runtime = AgentRuntime(
            model,
            registry,
            limits=ExecutionLimits(
                max_iterations=self.max_rounds,
                max_tool_calls=job_limits.max_tool_calls if job_limits else 40,
                model_timeout_seconds=config.AI_TIMEOUT_SECONDS,
                tool_timeout_seconds=config.AI_TIMEOUT_SECONDS,
                repeated_tool_call_limit=(job_limits.repeated_tool_call_limit if job_limits else None),
            ),
            hooks=_LegacyHooks(self),
            permissions=PermissionEngine(PermissionPolicy({name: "allow" for name in self.tools})),
            passthrough_exceptions=(ApprovalRequired, ExecutionLimitExceeded),
        )
        result = await self.runtime.run(
            self.agent_request,
            self.messages,
            generation_options={"think": self.think},
        )
        if result.status == "blocked":
            self.outcome = f"blocked: {result.error}"
        elif result.status == "timed_out":
            self.outcome = f"timed out: {result.error}"
        elif result.status == "failed" and self.outcome == "completed":
            self.outcome = result.error or "failed"
        if self.plan is not None and result.status in {"blocked", "failed", "timed_out"}:
            self.plan = Replanner().block(
                self.plan, f"run ended with status {result.status}"
            )
        return self.build_result(
            result.final_text,
            status=result.status,
            error=result.error,
            clarification=result.metadata.get("clarification"),
        )
