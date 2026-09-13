"""The bounded model/tool loop for one prepared AI request."""

import asyncio
import inspect
import time

from assistant.tasks.tools import (
    REMINDER_CREATION_TOOL_NAMES,
    reminder_creation_requested,
)
from workflows.runtime.context import ApprovalRequired, ExecutionLimitExceeded

from .. import client, config, response
from ..tooling.events import (
    audit_tool_arguments,
    emit_tool_event,
    tool_call_signature,
    tool_detail,
)


class ModelToolLoop:
    def __init__(
        self,
        *,
        session,
        messages: list[dict],
        tool_schemas: list[dict],
        tools: dict,
        max_rounds: int,
        prompt: str,
        mode: str,
        think: bool,
        reply_language,
        assistant_context,
        execution_context,
        tool_event_callback,
        progress,
        guard,
        build_result,
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
        self.outcome = "completed"

    def blocked_result(self, reason: str):
        self.outcome = f"blocked: {reason}"
        return self.build_result(reason, status="blocked", error=reason)

    async def run(self):
        tool_call_count = 0
        tool_call_counts: dict[str, int] = {}
        completed_tools: set[str] = set()
        for round_number in range(1, self.max_rounds + 1):
            reason = self.guard.limit_reason(tool_call_count, pending_model=True)
            if reason:
                return self.blocked_result(reason)
            await self.progress.emit(
                "model",
                f"waiting for AI (loop {round_number}/{self.max_rounds})",
                round_number,
            )
            data = await self.guard.wait(
                client.chat(
                    self.session,
                    self.messages,
                    self.tool_schemas,
                    self.think,
                )
            )
            self.progress.record_usage(data)
            await self.progress.emit()
            reason = self.guard.limit_reason(tool_call_count)
            if reason:
                return self.blocked_result(reason)
            message = data.get("message", {})
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                answer = message.get("content", "").strip()
                if not answer:
                    self.outcome = "AI returned an empty response"
                    return self.build_result(
                        "no response from ai",
                        status="failed",
                        error=self.outcome,
                    )
                if (
                    self.assistant_context is not None
                    and reminder_creation_requested(self.prompt)
                    and not completed_tools & REMINDER_CREATION_TOOL_NAMES
                ):
                    self.outcome = "failed: reminder tool was not completed"
                    text = (
                        "สร้างการแจ้งเตือนไม่สำเร็จ กรุณาลองอีกครั้ง"
                        if self.reply_language.code == "th"
                        else "I couldn't create the reminder. Please try again."
                    )
                    return self.build_result(
                        text,
                        status="failed",
                        error=self.outcome,
                    )
                answer = await self.guard.wait(
                    response.enforce_reply_language(
                        self.session,
                        answer,
                        self.reply_language,
                        self.progress,
                    )
                )
                reason = self.guard.limit_reason(tool_call_count)
                if reason:
                    return self.blocked_result(reason)
                return self.build_result(response.to_ascii_digits(answer))

            self.messages.append(message)
            for call in tool_calls:
                name = call["function"]["name"]
                args = call["function"].get("arguments") or {}
                tool_call_count += 1
                reason = self.guard.limit_reason(
                    tool_call_count,
                    pending_tool=True,
                )
                if reason:
                    return self.blocked_result(reason)
                signature = tool_call_signature(name, args)
                repeated = tool_call_counts.get(signature, 0) + 1
                tool_call_counts[signature] = repeated
                if (
                    self.execution_context is not None
                    and repeated
                    >= self.execution_context.limits.repeated_tool_call_limit
                ):
                    reason = (
                        f"repeated identical tool call detected: {name} "
                        f"({repeated} attempts)"
                    )
                    await emit_tool_event(
                        self.tool_event_callback,
                        {
                            "job_id": self.execution_context.job_id,
                            "tool_name": name,
                            "arguments": audit_tool_arguments(name, args),
                            "status": "blocked",
                            "elapsed_seconds": 0,
                            "result_size": 0,
                            "error": reason,
                        },
                    )
                    return self.blocked_result(reason)
                detail = tool_detail(name, args)
                await self.progress.emit("tool", detail, round_number)
                reason = self.guard.limit_reason(
                    tool_call_count,
                    pending_tool=True,
                )
                if reason:
                    return self.blocked_result(reason)
                if name in self.tools:
                    result, tool_outcome, tool_error, elapsed_ms = (
                        await self._execute_tool(name, args)
                    )
                    if tool_outcome == "finished":
                        completed_tools.add(name)
                else:
                    elapsed_ms = 0
                    tool_outcome = "blocked"
                    tool_error = (
                        f"tool is not allowed in {self.mode} mode: {name}"
                    )
                    result = tool_error
                await emit_tool_event(
                    self.tool_event_callback,
                    {
                        "job_id": (
                            self.execution_context.job_id
                            if self.execution_context is not None
                            else None
                        ),
                        "tool_name": name,
                        "arguments": audit_tool_arguments(name, args),
                        "status": tool_outcome,
                        "elapsed_seconds": elapsed_ms / 1000,
                        "result_size": len(str(result).encode("utf-8")),
                        "error": tool_error,
                    },
                )
                tool_message = {"role": "tool", "content": str(result)}
                if call.get("id"):
                    tool_message["tool_call_id"] = call["id"]
                self.messages.append(tool_message)
                await self.progress.emit(
                    "tool_done",
                    f"{detail}: {tool_outcome}",
                    round_number,
                )

        self.outcome = f"stopped after {self.max_rounds} tool loops"
        return self.build_result(
            "no response from ai",
            status="failed",
            error=self.outcome,
        )

    async def _execute_tool(self, name: str, args: dict):
        func = self.tools[name]
        tool_started = time.perf_counter()
        tool_outcome = "finished"
        tool_error = None
        try:
            result = (
                await self.guard.wait(func(**args))
                if inspect.iscoroutinefunction(func)
                else func(**args)
            )
        except (ExecutionLimitExceeded, ApprovalRequired):
            raise
        except (asyncio.TimeoutError, TimeoutError):
            tool_outcome = "timed out"
            tool_error = f"tool timed out: {name}"
            result = tool_error
        except Exception as error:
            tool_outcome = "failed"
            tool_error = f"{type(error).__name__}: {error}"
            config.debug(f"[tool] name={name} error={error!r}")
            result = f"tool failed: {name}: {error}"
        elapsed_ms = round((time.perf_counter() - tool_started) * 1000)
        config.debug(f"[timing] event=tool ms={elapsed_ms} name={name}")
        return result, tool_outcome, tool_error, elapsed_ms
