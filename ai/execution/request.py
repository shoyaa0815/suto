"""Build one model request from mode, identity, history, and tool policy."""

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from application.language import ReplyLanguage, choose_reply_language
from application.modes import get_mode_policy
from assistant.context import AssistantContext
from assistant.memory.service import PersistentMemory
from assistant.memory.tools import MEMORY_TOOL_NAMES
from context import ContextManager
from mcp_integration.config import MCPConfig
from retrieval.memory import MemoryRetriever
from skills import SkillRegistry, builtin_registry
from tools import (
    ADVANCED_TOOL_NAMES,
    COMMAND_TOOL_NAMES,
    PLANNING_TOOL_NAMES,
    TASK_TOOL_NAMES,
    get_tools,
)
from workflows.runtime.context import ALL_WORKSPACE_TOOLS, ExecutionContext

from .. import config, prompting

if TYPE_CHECKING:
    from mcp_integration.adapter import MCPTool


RETIRED_PREFERENCE_KEYS = frozenset(
    {"briefing_time", "briefing_delivery_target_id"}
)


def eligible_mcp_config(
    config: MCPConfig,
    execution_context: ExecutionContext | None,
    active_skills: tuple[str, ...],
    skill_registry: SkillRegistry | None,
) -> MCPConfig:
    """Start only servers with tools allowed for this request."""
    skills = (skill_registry or builtin_registry()).active(active_skills)
    selected = []
    for server in config.servers:
        allowed = set(server.allow_tools)
        if execution_context is not None:
            allowed = {
                name for name in allowed
                if f"mcp.{server.name}.{name}" in execution_context.allowed_tools
            }
        for skill in skills:
            if skill.allowed_tools is not None:
                allowed = {
                    name for name in allowed
                    if f"mcp.{server.name}.{name}" in skill.allowed_tools
                }
        if allowed:
            selected.append(replace(server, allow_tools=frozenset(allowed)))
    return MCPConfig(tuple(selected))


@dataclass(frozen=True)
class PreparedRequest:
    attachments: dict[str, tuple[str, bytes]]
    reply_language: ReplyLanguage
    allowed_tools: frozenset[str]
    tool_schemas: list[dict]
    messages: list[dict]
    max_tool_rounds: int


def _allowed_tools(
    mode: str,
    attachments: dict[str, tuple[str, bytes]],
    assistant_context: AssistantContext | None,
    execution_context: ExecutionContext | None,
) -> frozenset[str]:
    allowed_tools = get_mode_policy(mode).allowed_tools
    if not attachments:
        allowed_tools = allowed_tools - config.ATTACHMENT_TOOL_NAMES
    if assistant_context is None:
        allowed_tools = allowed_tools - TASK_TOOL_NAMES - MEMORY_TOOL_NAMES
    elif not assistant_context.allow_personal_tools:
        allowed_tools = allowed_tools - TASK_TOOL_NAMES
    job_scoped_tools = (
        ALL_WORKSPACE_TOOLS
        | PLANNING_TOOL_NAMES
        | COMMAND_TOOL_NAMES
        | ADVANCED_TOOL_NAMES
        | frozenset({"agent.delegate"})
    )
    if execution_context is None:
        return allowed_tools - (job_scoped_tools - {"agent.delegate"})
    allowed_job_tools = job_scoped_tools & execution_context.allowed_tools
    if execution_context.plan_store is None:
        allowed_job_tools = (
            allowed_job_tools - PLANNING_TOOL_NAMES - ADVANCED_TOOL_NAMES
        )
    if execution_context.command_event_callback is None:
        allowed_job_tools = allowed_job_tools - COMMAND_TOOL_NAMES
    return allowed_job_tools


def _personal_context(context: AssistantContext | None) -> str:
    if context is None:
        return ""
    user = context.store.get_user(context.user_id)
    if user is None:
        return ""
    delivery = None
    if context.default_delivery_target is not None:
        delivery = {
            "platform": context.default_delivery_target.platform,
            "default_destination": "private_dm",
            "current_channel_id": (
                context.current_delivery_target.destination_id
                if context.current_delivery_target is not None
                else None
            ),
            "current_channel_name": (
                context.current_delivery_target.display_name
                if context.current_delivery_target is not None
                else None
            ),
        }
    preferences = {
        key: value
        for key, value in context.store.user_preferences(user.id).items()
        if key not in RETIRED_PREFERENCE_KEYS
    }
    payload: dict = {
        "display_name": user.display_name,
        "timezone": user.timezone,
        "locale": user.locale,
        "preferences": preferences,
        "delivery": delivery,
    }
    if hasattr(context.store, "get_session_summary") and context.conversation_id:
        summary_obj = context.store.get_session_summary(context.conversation_id)
        if summary_obj and summary_obj.user_id == context.user_id and summary_obj.summary:
            payload["session_summary"] = ContextManager().summary(summary_obj.summary)

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )


def prepare_request(
    prompt: str,
    mode: str,
    attachments: dict[str, tuple[str, bytes]] | None,
    reply_language: ReplyLanguage | None,
    skill_instructions: str,
    conversation_history: list[dict[str, str]] | None,
    assistant_context: AssistantContext | None,
    execution_context: ExecutionContext | None,
    *,
    active_skills: tuple[str, ...] = (),
    skill_registry: SkillRegistry | None = None,
    mcp_tools: dict[str, "MCPTool"] | None = None,
) -> PreparedRequest:
    skills = (skill_registry or builtin_registry()).active(active_skills)
    policy = get_mode_policy(mode)
    selected_language = reply_language or choose_reply_language(prompt)
    request_attachments = attachments or {}
    native_tools = _allowed_tools(
        mode,
        request_attachments,
        assistant_context,
        execution_context,
    )
    permitted_mcp = frozenset(mcp_tools or {})
    if execution_context is not None:
        permitted_mcp &= execution_context.allowed_tools
    if native_tools & permitted_mcp:
        raise ValueError("MCP tool name collides with a native tool")
    allowed_tools = native_tools | permitted_mcp
    for skill in skills:
        if skill.allowed_tools is not None:
            allowed_tools &= frozenset(skill.allowed_tools)
    _, tool_schemas, tool_guidance = get_tools(allowed_tools & native_tools)
    tool_schemas.extend(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": mcp_tools[name].description,
                "parameters": mcp_tools[name].input_schema,
            },
        }
        for name in sorted(permitted_mcp & allowed_tools)
    )
    retrieved = []
    if assistant_context is not None and prompt.strip():
        retrieved = MemoryRetriever(
            PersistentMemory(assistant_context.store, assistant_context.user_id)
        ).search_sync(prompt, limit=5)
    if assistant_context is not None and not assistant_context.allow_personal_tools:
        tool_guidance += (
            "\n- Personal tasks and reminders in this CLI are managed only by "
            "/task and /reminder commands. Do not claim to have created, "
            "changed, or deleted one from chat."
        )
    messages = ContextManager().build(
        prompting.build_system_prompt(
            policy.prompt,
            tool_guidance,
            selected_language,
            personal_context=_personal_context(assistant_context),
        ),
        prompt,
        conversation_history,
        retrieved,
        active_skills=skills,
        available_tools=allowed_tools,
        legacy_skill_instructions=skill_instructions,
    )
    return PreparedRequest(
        attachments=request_attachments,
        reply_language=selected_language,
        allowed_tools=allowed_tools,
        tool_schemas=tool_schemas,
        messages=messages,
        max_tool_rounds=(
            config.MAX_AGENT_TOOL_ROUNDS
            if execution_context is not None
            else config.MAX_TOOL_ROUNDS
        ),
    )
