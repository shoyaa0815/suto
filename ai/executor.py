"""Public AI execution lifecycle and stable compatibility wrappers."""

import asyncio
import time
from dataclasses import replace
from collections.abc import Callable, Awaitable

import aiohttp

from agent import AgentRequest
from agent.events import AgentEvent
from application.language import ReplyLanguage, choose_reply_language
from application.modes import DEFAULT_MODE
from assistant.context import AssistantContext
from workflows.runtime.context import (
    ApprovalRequired,
    ExecutionContext,
    ExecutionLimitExceeded,
)

from . import client, config, prompting, response
from .execution.limits import ExecutionGuard
from .execution.loop import ModelToolLoop
from .execution.request import eligible_mcp_config, prepare_request
from .models import (
    AIExecutionResult,
    ChangeEventCallback,
    ProgressCallback,
    ToolEventCallback,
)
from .progress import RequestProgress
from retrieval.base import RetrievalError
from skills import SkillRegistry
from .tooling.assembly import build_runtime_tools
from mcp_integration import MCPManager, load_mcp_config


__all__ = [
    "ask_local_ai",
    "execute_local_ai",
]

# Preserve the module-level patch points available before the refactor.
_COMPAT_MODULES = (client, prompting, response)


async def execute_local_ai(
    prompt: str | AgentRequest,
    think: bool = False,
    mode: str = DEFAULT_MODE,
    attachments: dict[str, tuple[str, bytes]] | None = None,
    reply_language: ReplyLanguage | None = None,
    progress_callback: ProgressCallback | None = None,
    execution_context: ExecutionContext | None = None,
    tool_event_callback: ToolEventCallback | None = None,
    change_event_callback: ChangeEventCallback | None = None,
    skill_instructions: str = "",
    conversation_history: list[dict[str, str]] | None = None,
    assistant_context: AssistantContext | None = None,
    active_skills: tuple[str, ...] = (),
    skill_registry: SkillRegistry | None = None,
    agent_event_callback: Callable[[AgentEvent], Awaitable[None] | None] | None = None,
) -> AIExecutionResult:
    if isinstance(prompt, AgentRequest):
        request = replace(prompt, attachments=attachments) if attachments is not None else prompt
    else:
        request = AgentRequest(
            prompt,
            session_id=getattr(assistant_context, "conversation_id", None),
            attachments=attachments or {},
            active_skills=active_skills,
        )
    prompt = request.user_message
    attachments = request.attachments
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
        clarification: dict[str, list[str] | str] | None = None,
    ) -> AIExecutionResult:
        return AIExecutionResult(
            text=text,
            status=status,
            error=error,
            prompt_tokens=progress.prompt_tokens,
            output_tokens=progress.output_tokens,
            elapsed_seconds=time.perf_counter() - request_started,
            clarification=clarification,
        )

    def blocked_result(reason: str) -> AIExecutionResult:
        nonlocal outcome
        outcome = f"blocked: {reason}"
        return build_result(reason, status="blocked", error=reason)

    try:
        mcp_config = load_mcp_config()
    except ValueError:
        outcome = "invalid MCP configuration"
        await progress.emit("finished", outcome)
        text = (
            "การตั้งค่า MCP ไม่ถูกต้อง กรุณาตรวจสอบไฟล์การตั้งค่า"
            if (reply_language or choose_reply_language(prompt)).code == "th"
            else "MCP configuration is invalid. Check the configuration file."
        )
        return build_result(text, status="failed", error=outcome)

    mcp_config = eligible_mcp_config(
        mcp_config, execution_context, request.active_skills, skill_registry
    )
    mcp_manager = MCPManager(mcp_config)
    try:
        await mcp_manager.start()
        prepared = prepare_request(
            prompt,
            mode,
            attachments,
            reply_language,
            skill_instructions,
            conversation_history,
            assistant_context,
            execution_context,
            active_skills=request.active_skills,
            skill_registry=skill_registry,
            mcp_tools=mcp_manager.allowed_tools(),
        )
    except RetrievalError:
        await mcp_manager.close()
        outcome = "memory retrieval unavailable"
        config.debug("[ai] memory retrieval unavailable")
        await progress.emit("finished", outcome)
        language = reply_language or choose_reply_language(prompt)
        text = (
            "ค้นความจำไม่พร้อมใช้งาน กรุณาลองใหม่อีกครั้ง"
            if language.code == "th"
            else "Memory search is unavailable. Please try again."
        )
        return build_result(text, status="failed", error=outcome)
    except BaseException:
        await mcp_manager.close()
        raise
    try:
        guard = ExecutionGuard(execution_context, progress, request_started)
        timeout = aiohttp.ClientTimeout(
            total=config.AI_TIMEOUT_SECONDS,
            connect=10,
            sock_read=config.AI_TIMEOUT_SECONDS,
        )
        progress.start()
        await progress.emit("starting", f"starting {mode} request")
        for server_name in mcp_manager.failures:
            await progress.emit("mcp", f"MCP server unavailable: {server_name}")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tools = build_runtime_tools(
                session=session,
                allowed_tools=prepared.allowed_tools - mcp_manager.allowed_tools().keys(),
                attachments=prepared.attachments,
                assistant_context=assistant_context,
                execution_context=execution_context,
                change_event_callback=change_event_callback,
                guard=guard,
                progress=progress,
            )
            loop = ModelToolLoop(
                session=session,
                messages=prepared.messages,
                tool_schemas=prepared.tool_schemas,
                tools=tools,
                max_rounds=prepared.max_tool_rounds,
                prompt=prompt,
                mode=mode,
                think=think,
                reply_language=prepared.reply_language,
                assistant_context=assistant_context,
                execution_context=execution_context,
                tool_event_callback=tool_event_callback,
                progress=progress,
                guard=guard,
                build_result=build_result,
                agent_request=request,
                skill_registry=skill_registry,
                agent_event_callback=agent_event_callback,
                mcp_tools={
                    name: tool for name, tool in mcp_manager.allowed_tools().items()
                    if name in prepared.allowed_tools
                },
            )
            result = await loop.run()
            outcome = loop.outcome
            return replace(result, plan=loop.plan)
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
        config.debug(f"[ai] connection failed: {type(error).__name__}")
        return build_result(
            "can't connect check ai server",
            status="failed",
            error=outcome,
        )
    except (asyncio.TimeoutError, TimeoutError):
        outcome = f"timed out after {config.AI_TIMEOUT_SECONDS}s"
        config.debug(f"[ai] timeout after {config.AI_TIMEOUT_SECONDS}s")
        if prepared.reply_language.code == "th":
            text = "AI ใช้เวลาประมวลผลนานเกินไป กรุณาลองใหม่อีกครั้ง"
        else:
            text = "AI processing timed out. Please try again."
        return build_result(text, status="timed_out", error=outcome)
    except Exception as error:
        outcome = f"failed: {type(error).__name__}"
        config.debug(f"[ai] {type(error).__name__}")
        text = (
            "คำขอ AI ล้มเหลว กรุณาลองใหม่อีกครั้ง"
            if prepared.reply_language.code == "th"
            else "AI request failed. Please try again."
        )
        return build_result(
            text,
            status="failed",
            error=outcome,
        )
    finally:
        await mcp_manager.close()
        await progress.stop()
        await progress.emit("finished", outcome)
        elapsed_ms = round((time.perf_counter() - request_started) * 1000)
        config.debug(
            f"[timing] event=request ms={elapsed_ms} mode={mode} "
            f"attachments={len(prepared.attachments)}"
        )


async def ask_local_ai(
    prompt: str,
    think: bool = False,
    mode: str = DEFAULT_MODE,
    attachments: dict[str, tuple[str, bytes]] | None = None,
    reply_language: ReplyLanguage | None = None,
    progress_callback: ProgressCallback | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    assistant_context: AssistantContext | None = None,
    active_skills: tuple[str, ...] = (),
    skill_registry: SkillRegistry | None = None,
) -> str:
    """Compatibility wrapper for chat interfaces that only need answer text."""
    result = await execute_local_ai(
        prompt,
        think=think,
        mode=mode,
        attachments=attachments,
        reply_language=reply_language,
        progress_callback=progress_callback,
        conversation_history=conversation_history,
        assistant_context=assistant_context,
        active_skills=active_skills,
        skill_registry=skill_registry,
    )
    return result.text
