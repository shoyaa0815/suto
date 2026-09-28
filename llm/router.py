"""Provider-independent routing for model requests."""

from .base import Model
from .types import ModelRequest, ModelResponse


class ModelRouter:
    def __init__(self, default_model: Model) -> None:
        self.default_model = default_model

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return await self.default_model.generate(request)
