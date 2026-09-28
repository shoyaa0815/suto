"""Adapter from the existing chat entry point to normalized model values."""

from llm.types import ModelRequest, ModelResponse, ModelUsage, ToolCall

from .. import client


class ChatModelAdapter:
    def __init__(self, session) -> None:
        self.session = session

    async def generate(self, request: ModelRequest) -> ModelResponse:
        data = await client.chat(
            self.session,
            request.messages,
            request.available_tools,
            think=bool(request.generation_options.get("think", False)),
        )
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
            finish_reason="tool_calls" if calls else "stop",
            usage=ModelUsage(
                int(data.get("prompt_eval_count") or 0),
                int(data.get("eval_count") or 0),
            ),
            assistant_message=message,
        )
