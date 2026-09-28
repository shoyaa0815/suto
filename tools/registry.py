"""Request-scoped tool registration and argument validation."""

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .base import Tool
from .types import ToolResult


class ToolValidationError(ValueError):
    pass


def _validate_value(value: Any, schema: dict[str, Any], location: str) -> None:
    kind = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if kind not in checks or not checks[kind](value):
        raise ToolValidationError(f"{location} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError(f"{location} must be one of the allowed values")
    if kind == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ToolValidationError(f"{location}.{name} is required")
        if schema.get("additionalProperties") is False:
            unknown = value.keys() - properties.keys()
            if unknown:
                raise ToolValidationError(f"{location} has unknown arguments")
        for name, item in value.items():
            if name in properties:
                _validate_value(item, properties[name], f"{location}.{name}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ToolValidationError(f"{location} has too few items")
        if len(value) > schema.get("maxItems", float("inf")):
            raise ToolValidationError(f"{location} has too many items")
        if schema.get("uniqueItems") and len({
            json.dumps(item, sort_keys=True) for item in value
        }) != len(value):
            raise ToolValidationError(f"{location} contains duplicate items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_value(item, schema["items"], f"{location}[{index}]")
    elif kind == "string" and "pattern" in schema:
        if re.search(schema["pattern"], value) is None:
            raise ToolValidationError(f"{location} has an invalid format")
    elif kind in {"integer", "number"}:
        if value < schema.get("minimum", float("-inf")):
            raise ToolValidationError(f"{location} is below the minimum")
        if value > schema.get("maximum", float("inf")):
            raise ToolValidationError(f"{location} is above the maximum")


@dataclass
class FunctionTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        result = self.handler(**args)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, ToolResult) else ToolResult(ok=True, content=result)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        if tool.input_schema.get("type") != "object":
            raise ValueError(f"tool requires an object input schema: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        del self._tools[name]

    def resolve(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def export_model_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools.values()
        ]

    def validate(self, tool: Tool, arguments: Any) -> dict[str, Any]:
        _validate_value(arguments, tool.input_schema, "arguments")
        return arguments
