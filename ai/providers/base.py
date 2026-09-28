from typing import Protocol

import aiohttp
from llm.base import Model
from llm.types import ModelResponse, ModelUsage, ToolCall


class ProviderTransientError(RuntimeError):
    pass


class ChatProvider(Protocol):
    name: str

    async def chat(
        self,
        session: aiohttp.ClientSession,
        messages: list,
        tool_schemas: list,
        think: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict: ...


class ChatModelProvider(ChatProvider, Model, Protocol):
    """A provider serving both the runtime and legacy chat callers."""


def raise_for_provider_status(response) -> None:
    if response.status == 429 or response.status >= 500:
        raise ProviderTransientError(
            f"provider returned transient HTTP status {response.status}"
        )
    response.raise_for_status()


def model_response(data: dict) -> ModelResponse:
    """Convert the existing chat shape to the runtime's provider-neutral result."""
    message = data.get("message") or {}
    calls = []
    for raw_call in message.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("provider returned a tool call without a name")
        arguments = function.get("arguments")
        calls.append(ToolCall(name, {} if arguments is None else arguments, raw_call.get("id")))
    return ModelResponse(
        text=message.get("content") or "",
        tool_calls=calls,
        finish_reason=data.get("finish_reason") or ("tool_calls" if calls else "stop"),
        usage=ModelUsage(
            int(data.get("prompt_eval_count") or 0),
            int(data.get("eval_count") or 0),
        ),
        assistant_message=message,
    )
