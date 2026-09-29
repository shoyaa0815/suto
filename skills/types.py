"""Validated skill data; capabilities remain in the tool registry."""

import json
import re
from dataclasses import dataclass, field
from typing import Any


_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_TOOL = re.compile(r"[a-z][a-z0-9_.-]*\Z")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    recommended_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError("skill name must use lowercase letters, digits, '-' or '_'")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("skill description is required")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("skill instructions are required")
        for field_name, names in (
            ("recommended_tools", self.recommended_tools),
            ("allowed_tools", self.allowed_tools),
        ):
            if names is None and field_name == "allowed_tools":
                continue
            if not isinstance(names, tuple) or any(
                not isinstance(name, str) or not _TOOL.fullmatch(name) for name in names
            ):
                raise ValueError(f"{field_name} must be a list of tool names")
            if len(set(names)) != len(names):
                raise ValueError(f"{field_name} contains duplicate tools")
        if not isinstance(self.configuration, dict) or not isinstance(self.metadata, dict):
            raise ValueError("skill configuration and metadata must be mappings")
        for field_name, value in (("configuration", self.configuration), ("metadata", self.metadata)):
            if any(not isinstance(key, str) for key in value):
                raise ValueError(f"skill {field_name} keys must be strings")
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise ValueError(f"skill {field_name} must contain JSON values") from error
