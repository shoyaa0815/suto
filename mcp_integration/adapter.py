"""Translate discovered MCP tools to the existing Suto Tool contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from tools.types import ToolResult


_TOOL_NAME = re.compile(r"[a-z][a-z0-9_.-]*\Z")
_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "uniqueItems", "enum", "pattern", "minimum", "maximum",
    "description", "title", "default", "examples",
}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _field(source: Any, key: str, fallback: str | None = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, source.get(fallback) if fallback else None)
    return getattr(source, key, getattr(source, fallback, None) if fallback else None)


def _supported_schema(schema: Any) -> bool:
    if not isinstance(schema, dict) or set(schema) - _KEYWORDS or schema.get("type") not in _TYPES:
        return False
    kind = schema["type"]
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not all(
            isinstance(name, str) and _supported_schema(value)
            for name, value in properties.items()
        ):
            return False
        if not isinstance(required, list) or any(name not in properties for name in required):
            return False
        if not isinstance(schema.get("additionalProperties", True), bool):
            return False
    elif any(key in schema for key in ("properties", "required", "additionalProperties")):
        return False
    if kind == "array":
        if "items" in schema and not _supported_schema(schema["items"]):
            return False
    elif any(key in schema for key in ("items", "minItems", "maxItems", "uniqueItems")):
        return False
    if kind != "string" and "pattern" in schema:
        return False
    if kind not in {"integer", "number"} and any(key in schema for key in ("minimum", "maximum")):
        return False
    return True


def _content(block: Any) -> Any:
    if _field(block, "type") == "text":
        return _field(block, "text")
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return block


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    server: str
    original_name: str
    client: Any

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        metadata = {"mcp_server": self.server, "mcp_tool": self.original_name}
        try:
            result = await self.client.call_tool(self.original_name, args)
            if _field(result, "isError", "is_error"):
                return ToolResult(False, "MCP tool reported an error", metadata, "mcp_tool_error")
            structured = _field(result, "structuredContent", "structured_content")
            if structured is not None:
                content = structured
            else:
                blocks = [_content(block) for block in (_field(result, "content") or [])]
                content = blocks[0] if len(blocks) == 1 else blocks
            return ToolResult(True, content, metadata)
        except Exception:
            # Transport exceptions may include secrets or private server output.
            return ToolResult(False, "MCP server call failed", metadata, "mcp_server_error")


def adapt_tool(server: str, definition: Any, client: Any) -> MCPTool:
    original = _field(definition, "name")
    description = _field(definition, "description")
    schema = _field(definition, "inputSchema", "input_schema")
    if not isinstance(original, str) or not _TOOL_NAME.fullmatch(original):
        raise ValueError("invalid MCP tool name")
    if description is not None and not isinstance(description, str):
        raise ValueError("invalid MCP tool description")
    if not isinstance(schema, dict):
        raise ValueError("invalid MCP input schema")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        raise ValueError("invalid MCP input schema") from error
    if schema.get("type") != "object" or not _supported_schema(schema):
        raise ValueError("unsupported MCP input schema")
    return MCPTool(f"mcp.{server}.{original}", description or "", schema, server, original, client)
