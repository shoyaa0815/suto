import asyncio
import inspect
import time

import aiohttp

from automation.context import (
    ALL_WORKSPACE_TOOLS,
    ApprovalRequired,
    ExecutionContext,
    ExecutionLimitExceeded,
)
from core.language import ReplyLanguage, choose_reply_language
from core.modes import DEFAULT_MODE, get_mode_policy
from tools import (
    ADVANCED_TOOL_NAMES,
    build_advanced_tools,
    COMMAND_TOOL_NAMES,
    PLANNING_TOOL_NAMES,
    build_attachment_tools,
    build_command_tools,
    build_planning_tools,
    build_workspace_tools,
    get_tools,
)

from . import client, config, prompting, response
from .models import (
    AIExecutionResult,
    ChangeEventCallback,
    ProgressCallback,
    ToolEventCallback,
)
from .progress import RequestProgress
from .tool_runtime import (
    audit_tool_arguments,
    emit_tool_event,
    tool_call_signature,
    tool_detail,
)


async def execute_local_ai(
    prompt: str,
    think: bool = False,
    mode: str = DEFAULT_MODE,
    attachments: dict[str, tuple[str, bytes]] | None = None,
    reply_language: ReplyLanguage | None = None,
    progress_callback: ProgressCallback | None = None,
    execution_context: ExecutionContext | None = None,
    tool_event_callback: ToolEventCallback | None = None,
    change_event_callback: ChangeEventCallback | None = None,
    skill_instructions: str = "",
) -> AIExecutionResult:
    request_started = time.perf_counter()
    max_tool_rounds = (
        config.MAX_AGENT_TOOL_ROUNDS
        if execution_context is not None
        else config.MAX_TOOL_ROUNDS
    )
    progress = RequestProgress(
        progress_callback,
        request_started,
        max_rounds=max_tool_rounds,
    )
    outcome = "completed"

    def build_result(
        text: str,
        status: str = "completed",
        error: str | None = None,
    ) -> AIExecutionResult:
        return AIExecutionResult(
            text=text,
            status=status,
            error=error,
            prompt_tokens=progress.prompt_tokens,
            output_tokens=progress.output_tokens,
            elapsed_seconds=time.perf_counter() - request_started,
        )

    def execution_limit_reason(tool_calls: int = 0, *, pending_tool: bool = False,
                               pending_model: bool = False) -> str | None:
        if execution_context is None:
            return None
        limits = execution_context.limits
        if execution_context.budget_check is not None:
            if reason := execution_context.budget_check(pending_tool=pending_tool, pending_model=pending_model):
                return reason
        elapsed = time.perf_counter() - request_started
        if elapsed >= limits.max_elapsed_seconds:
            return (
                "job elapsed-time limit reached "
                f"({limits.max_elapsed_seconds:g} seconds)"
            )
        if progress.total_tokens > limits.max_tokens:
            return f"job token limit exceeded ({limits.max_tokens} tokens)"
        if tool_calls > limits.max_tool_calls:
            return f"job tool-call limit exceeded ({limits.max_tool_calls} calls)"
        return None

    def blocked_result(reason: str) -> AIExecutionResult:
        nonlocal outcome
        outcome = f"blocked: {reason}"
        return build_result(reason, status="blocked", error=reason)

    async def await_with_execution_deadline(awaitable):
        if execution_context is None:
            return await awaitable
        limit = execution_context.limits.max_elapsed_seconds
        remaining = limit - (time.perf_counter() - request_started)
        if execution_context.budget_seconds is not None:
            remaining = min(remaining, execution_context.budget_seconds())
        if remaining <= 0:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise ExecutionLimitExceeded(
                f"job elapsed-time limit reached ({limit:g} seconds)"
            )
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError as error:
            if (time.perf_counter() - request_started >= limit or
                    (execution_context.budget_seconds is not None and execution_context.budget_seconds() <= 0)):
                raise ExecutionLimitExceeded(
                    f"job elapsed-time limit reached ({limit:g} seconds)"
                ) from error
            raise

    policy = get_mode_policy(mode)
    reply_language = reply_language or choose_reply_language(prompt)
    attachments = attachments or {}
    allowed_tools = policy.allowed_tools
    if not attachments:
        allowed_tools = allowed_tools - config.ATTACHMENT_TOOL_NAMES
    job_scoped_tools = (
        ALL_WORKSPACE_TOOLS | PLANNING_TOOL_NAMES | COMMAND_TOOL_NAMES | ADVANCED_TOOL_NAMES
    )
    if execution_context is None:
        allowed_tools = allowed_tools - job_scoped_tools
    else:
        allowed_job_tools = job_scoped_tools & execution_context.allowed_tools
        if execution_context.plan_store is None:
            allowed_job_tools = allowed_job_tools - PLANNING_TOOL_NAMES - ADVANCED_TOOL_NAMES
        if execution_context.command_event_callback is None:
            allowed_job_tools = allowed_job_tools - COMMAND_TOOL_NAMES
        allowed_tools = (allowed_tools - job_scoped_tools) | allowed_job_tools

    _, tool_schemas, tool_guidance = get_tools(allowed_tools)
    timeout = aiohttp.ClientTimeout(
        total=config.AI_TIMEOUT_SECONDS,
        connect=10,
        sock_read=config.AI_TIMEOUT_SECONDS,
    )
    messages = [
        {
            "role": "system",
            "content": prompting.build_system_prompt(
                policy.prompt,
                tool_guidance,
                reply_language,
                skill_instructions,
            ),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        progress.start()
        await progress.emit("starting", f"starting {mode} request")
        async with aiohttp.ClientSession(timeout=timeout) as session:

            async def complete_document_part(
                system_prompt: str,
                content: str,
                max_output_tokens: int,
            ) -> str:
                data = await await_with_execution_deadline(
                    client.chat(
                        session,
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": content},
                        ],
                        [],
                        think=False,
                        max_output_tokens=max_output_tokens,
                    )
                )
                progress.record_usage(data)
                await progress.emit()
                return data.get("message", {}).get("content", "").strip()

            runtime_handlers = build_attachment_tools(
                attachments,
                complete_document_part,
            )
            if execution_context is not None:
                runtime_handlers.update(
                    build_workspace_tools(
                        execution_context,
                        change_event_callback,
                    )
                )
                if execution_context.plan_store is not None:
                    runtime_handlers.update(build_advanced_tools(execution_context))
                    runtime_handlers.update(
                        build_planning_tools(
                            execution_context,
                            execution_context.plan_store,
                        )
                    )
                if execution_context.command_event_callback is not None:
                    runtime_handlers.update(
                        build_command_tools(
                            execution_context,
                            execution_context.command_event_callback,
                        )
                    )
            tools, _, _ = get_tools(allowed_tools, runtime_handlers)
            tool_call_count = 0
            tool_call_counts: dict[str, int] = {}
            for round_number in range(1, max_tool_rounds + 1):
                if reason := execution_limit_reason(tool_call_count, pending_model=True):
                    return blocked_result(reason)
                await progress.emit(
                    "model",
                    f"waiting for AI (loop {round_number}/{max_tool_rounds})",
                    round_number,
                )
                data = await await_with_execution_deadline(
                    client.chat(session, messages, tool_schemas, think)
                )
                progress.record_usage(data)
                await progress.emit()
                if reason := execution_limit_reason(tool_call_count):
                    return blocked_result(reason)
                message = data.get("message", {})
                tool_calls = message.get("tool_calls")

                if not tool_calls:
                    answer = message.get("content", "").strip()
                    if not answer:
                        outcome = "AI returned an empty response"
                        return build_result(
                            "no response from ai",
                            status="failed",
                            error=outcome,
                        )
                    answer = await await_with_execution_deadline(
                        response.enforce_reply_language(
                            session,
                            answer,
                            reply_language,
                            progress,
                        )
                    )
                    if reason := execution_limit_reason(tool_call_count):
                        return blocked_result(reason)
                    return build_result(response.to_ascii_digits(answer))

                messages.append(message)
                for call in tool_calls:
                    name = call["function"]["name"]
                    args = call["function"].get("arguments") or {}
                    tool_call_count += 1
                    if reason := execution_limit_reason(tool_call_count, pending_tool=True):
                        return blocked_result(reason)
                    signature = tool_call_signature(name, args)
                    repeated = tool_call_counts.get(signature, 0) + 1
                    tool_call_counts[signature] = repeated
                    if (
                        execution_context is not None
                        and repeated
                        >= execution_context.limits.repeated_tool_call_limit
                    ):
                        reason = (
                            f"repeated identical tool call detected: {name} "
                            f"({repeated} attempts)"
                        )
                        await emit_tool_event(
                            tool_event_callback,
                            {
                                "job_id": execution_context.job_id,
                                "tool_name": name,
                                "arguments": audit_tool_arguments(name, args),
                                "status": "blocked",
                                "elapsed_seconds": 0,
                                "result_size": 0,
                                "error": reason,
                            },
                        )
                        return blocked_result(reason)
                    detail = tool_detail(name, args)
                    await progress.emit(
                        "tool",
                        f"running {detail}",
                        round_number,
                    )
                    if reason := execution_limit_reason(tool_call_count, pending_tool=True):
                        return blocked_result(reason)
                    if name in tools:
                        func = tools[name]
                        tool_started = time.perf_counter()
                        tool_outcome = "finished"
                        tool_error = None
                        try:
                            result = (
                                await await_with_execution_deadline(func(**args))
                                if inspect.iscoroutinefunction(func)
                                else func(**args)
                            )
                        except (ExecutionLimitExceeded, ApprovalRequired):
                            raise
                        except (asyncio.TimeoutError, TimeoutError):
                            tool_outcome = "timed out"
                            tool_error = f"tool timed out: {name}"
                            result = f"tool timed out: {name}"
                        except Exception as error:
                            tool_outcome = "failed"
                            tool_error = f"{type(error).__name__}: {error}"
                            config.debug(f"[tool] name={name} error={error!r}")
                            result = f"tool failed: {name}: {error}"
                        elapsed_ms = round(
                            (time.perf_counter() - tool_started) * 1000
                        )
                        config.debug(
                            f"[timing] event=tool ms={elapsed_ms} name={name}"
                        )
                    else:
                        elapsed_ms = 0
                        tool_outcome = "blocked"
                        tool_error = f"tool is not allowed in {mode} mode: {name}"
                        result = f"tool is not allowed in {mode} mode: {name}"
                    await emit_tool_event(
                        tool_event_callback,
                        {
                            "job_id": (
                                execution_context.job_id
                                if execution_context is not None
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
                    messages.append(tool_message)
                    await progress.emit(
                        "tool_done",
                        f"{tool_outcome} {detail} in {elapsed_ms / 1000:.1f}s",
                        round_number,
                    )

            outcome = f"stopped after {max_tool_rounds} tool loops"
            return build_result(
                "no response from ai",
                status="failed",
                error=outcome,
            )
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except ExecutionLimitExceeded as error:
        return blocked_result(str(error))
    except ApprovalRequired as error:
        outcome = f"waiting for approval: {error.approval_id}"
        return build_result(
            str(error),
            status="waiting_approval",
            error=str(error),
        )
    except aiohttp.ClientConnectorError as error:
        outcome = "AI server connection failed"
        config.debug(f"[ai] connection failed: {error!r}")
        return build_result(
            "can't connect check ai server",
            status="failed",
            error=outcome,
        )
    except (asyncio.TimeoutError, TimeoutError) as error:
        outcome = f"timed out after {config.AI_TIMEOUT_SECONDS}s"
        config.debug(
            f"[ai] timeout after {config.AI_TIMEOUT_SECONDS}s: {error!r}"
        )
        if reply_language.code == "th":
            text = "AI ใช้เวลาประมวลผลนานเกินไป กรุณาลองใหม่อีกครั้ง"
        else:
            text = "AI processing timed out. Please try again."
        return build_result(text, status="timed_out", error=outcome)
    except Exception as error:
        outcome = f"failed: {type(error).__name__}"
        config.debug(f"[ai] {type(error).__name__}: {error!r}")
        return build_result(
            f"error: {error}",
            status="failed",
            error=outcome,
        )
    finally:
        await progress.stop()
        await progress.emit("finished", outcome)
        elapsed_ms = round((time.perf_counter() - request_started) * 1000)
        config.debug(
            f"[timing] event=request ms={elapsed_ms} mode={mode} "
            f"attachments={len(attachments)}"
        )


async def ask_local_ai(
    prompt: str,
    think: bool = False,
    mode: str = DEFAULT_MODE,
    attachments: dict[str, tuple[str, bytes]] | None = None,
    reply_language: ReplyLanguage | None = None,
    progress_callback: ProgressCallback | None = None,
) -> str:
    """Compatibility wrapper for chat clients that only need answer text."""
    result = await execute_local_ai(
        prompt,
        think=think,
        mode=mode,
        attachments=attachments,
        reply_language=reply_language,
        progress_callback=progress_callback,
    )
    return result.text
