"""Assemble request-scoped tool handlers without widening mode policy."""

from assistant.context import AssistantContext
from tools import (
    build_advanced_tools,
    build_attachment_tools,
    build_command_tools,
    build_planning_tools,
    build_task_tools,
    build_workspace_tools,
    get_tools,
)
from workflows.runtime.context import ExecutionContext

from .. import client


def build_runtime_tools(
    *,
    session,
    allowed_tools: frozenset[str],
    attachments: dict[str, tuple[str, bytes]],
    assistant_context: AssistantContext | None,
    execution_context: ExecutionContext | None,
    change_event_callback,
    guard,
    progress,
) -> dict:
    async def complete_document_part(
        system_prompt: str,
        content: str,
        max_output_tokens: int,
    ) -> str:
        data = await guard.wait(
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

    handlers = build_attachment_tools(attachments, complete_document_part)
    if assistant_context is not None:
        handlers.update(build_task_tools(assistant_context))
    if execution_context is not None:
        handlers.update(
            build_workspace_tools(execution_context, change_event_callback)
        )
        if execution_context.plan_store is not None:
            handlers.update(build_advanced_tools(execution_context))
            handlers.update(
                build_planning_tools(
                    execution_context,
                    execution_context.plan_store,
                )
            )
        if execution_context.command_event_callback is not None:
            handlers.update(
                build_command_tools(
                    execution_context,
                    execution_context.command_event_callback,
                )
            )
    tools, _, _ = get_tools(allowed_tools, handlers)
    return tools
