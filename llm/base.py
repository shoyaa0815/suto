from typing import Protocol

from .types import ModelRequest, ModelResponse


class Model(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
