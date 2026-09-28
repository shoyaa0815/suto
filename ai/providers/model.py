"""Adapter from the existing chat entry point to normalized model values."""

from llm.types import ModelRequest, ModelResponse

from .. import client
from .base import model_response


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
        return model_response(data)
