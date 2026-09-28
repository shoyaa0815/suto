"""A model-callable tool's public contract."""

from typing import Any, Protocol

from .types import ToolResult


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    async def execute(self, args: dict[str, Any]) -> ToolResult: ...
