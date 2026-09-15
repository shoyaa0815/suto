"""Build one model request from mode, identity, history, and tool policy."""

import json
from dataclasses import dataclass

from application.language import ReplyLanguage, choose_reply_language
from application.modes import get_mode_policy
from assistant.context import AssistantContext
from tools import (
    ADVANCED_TOOL_NAMES,
    COMMAND_TOOL_NAMES,
    PLANNING_TOOL_NAMES,
    TASK_TOOL_NAMES,
    get_tools,
)
from workflows.runtime.context import ALL_WORKSPACE_TOOLS, ExecutionContext

from .. import config, prompting


RETIRED_PREFERENCE_KEYS = frozenset(
    {"briefing_time", "briefing_delivery_target_id"}
)


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
        allowed_tools = allowed_tools - TASK_TOOL_NAMES
    job_scoped_tools = (
        ALL_WORKSPACE_TOOLS
        | PLANNING_TOOL_NAMES
        | COMMAND_TOOL_NAMES
        | ADVANCED_TOOL_NAMES
    )
    if execution_context is None:
        return allowed_tools - job_scoped_tools
    allowed_job_tools = job_scoped_tools & execution_context.allowed_tools
    if execution_context.plan_store is None:
        allowed_job_tools = (
            allowed_job_tools - PLANNING_TOOL_NAMES - ADVANCED_TOOL_NAMES
        )
    if execution_context.command_event_callback is None:
        allowed_job_tools = allowed_job_tools - COMMAND_TOOL_NAMES
    return (allowed_tools - job_scoped_tools) | allowed_job_tools


def _history_messages(history: list[dict[str, str]] | None) -> list[dict]:
    return [
        {"role": item["role"], "content": item["content"]}
        for item in (history or [])[-20:]
        if item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str)
        and item["content"]
    ]


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
    return json.dumps(
        {
            "display_name": user.display_name,
            "timezone": user.timezone,
            "locale": user.locale,
            "preferences": preferences,
            "delivery": delivery,
        },
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
) -> PreparedRequest:
    policy = get_mode_policy(mode)
    selected_language = reply_language or choose_reply_language(prompt)
    request_attachments = attachments or {}
    allowed_tools = _allowed_tools(
        mode,
        request_attachments,
        assistant_context,
        execution_context,
    )
    _, tool_schemas, tool_guidance = get_tools(allowed_tools)
    messages = [
        {
            "role": "system",
            "content": prompting.build_system_prompt(
                policy.prompt,
                tool_guidance,
                selected_language,
                skill_instructions,
                _personal_context(assistant_context),
            ),
        },
        *_history_messages(conversation_history),
        {"role": "user", "content": prompt},
    ]
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
