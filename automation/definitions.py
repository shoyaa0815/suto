import json
import re
from pathlib import Path
from typing import Any

from .models import AutomationVersion


NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
SUPPORTED_PARAMETER_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "array", "object"}
)
MAX_TEMPLATE_CHARS = 50_000
MAX_SKILL_CHARS = 30_000
MAX_PARAMETERS = 50
SENSITIVE_NAMES = frozenset(
    {"secret", "password", "passwd", "token", "api_key", "apikey", "credential"}
)


def _is_sensitive_name(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    return any(
        normalized == marker or normalized.endswith(f"_{marker}")
        for marker in SENSITIVE_NAMES
    )


def validate_name(name: str, label: str = "name") -> str:
    normalized = str(name).strip().casefold()
    if not NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{label} must start with a letter and contain only lowercase "
            "letters, numbers, underscores, or hyphens (maximum 64 characters)"
        )
    return normalized


def validate_workspace(workspace: str | Path) -> str:
    resolved = Path(workspace).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"workspace is not a directory: {resolved}")
    return str(resolved)


def validate_parameter_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if schema is None:
        return {}
    if not isinstance(schema, dict):
        raise ValueError("parameter_schema must be an object")
    if len(schema) > MAX_PARAMETERS:
        raise ValueError(
            f"parameter_schema cannot contain more than {MAX_PARAMETERS} fields"
        )
    normalized: dict[str, Any] = {}
    for raw_name, raw_rule in schema.items():
        name = str(raw_name)
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            raise ValueError(f"invalid parameter name: {name}")
        if not isinstance(raw_rule, dict):
            raise ValueError(f"parameter rule must be an object: {name}")
        unknown = set(raw_rule) - {"type", "required", "default", "enum"}
        if unknown:
            raise ValueError(
                f"unknown parameter rule for {name}: {', '.join(sorted(unknown))}"
            )
        parameter_type = raw_rule.get("type", "string")
        if parameter_type not in SUPPORTED_PARAMETER_TYPES:
            raise ValueError(f"unsupported parameter type for {name}: {parameter_type}")
        if "required" in raw_rule and not isinstance(raw_rule["required"], bool):
            raise ValueError(f"required must be boolean: {name}")
        rule: dict[str, Any] = {
            "type": parameter_type,
            "required": bool(raw_rule.get("required", False)),
        }
        if "enum" in raw_rule:
            if not isinstance(raw_rule["enum"], list) or not raw_rule["enum"]:
                raise ValueError(f"enum must be a non-empty array: {name}")
            if any(
                not _matches_type(item, parameter_type)
                for item in raw_rule["enum"]
            ):
                raise ValueError(
                    f"enum values must match type {parameter_type}: {name}"
                )
            rule["enum"] = raw_rule["enum"]
        if "default" in raw_rule:
            if _is_sensitive_name(name):
                raise ValueError(
                    f"sensitive parameter defaults are not allowed: {name}"
                )
            rule["default"] = raw_rule["default"]
            _validate_value(name, rule["default"], rule)
        normalized[name] = rule
    return normalized


def validate_prompt_template(template: str, schema: dict[str, Any]) -> str:
    template = str(template).strip()
    if not template:
        raise ValueError("prompt_template cannot be empty")
    if len(template) > MAX_TEMPLATE_CHARS:
        raise ValueError(f"prompt_template exceeds {MAX_TEMPLATE_CHARS} characters")
    placeholders = set(PLACEHOLDER_PATTERN.findall(template))
    unknown = placeholders - set(schema)
    if unknown:
        raise ValueError(
            "template contains undeclared parameters: " + ", ".join(sorted(unknown))
        )
    unsafe_optional = {
        name
        for name in placeholders
        if not schema[name].get("required") and "default" not in schema[name]
    }
    if unsafe_optional:
        raise ValueError(
            "template parameters must be required or have defaults: "
            + ", ".join(sorted(unsafe_optional))
        )
    residue = PLACEHOLDER_PATTERN.sub("", template)
    if "{{" in residue or "}}" in residue:
        raise ValueError("prompt_template contains an invalid placeholder")
    return template


def validate_skill_instructions(instructions: str) -> str:
    normalized = str(instructions).strip()
    if not normalized:
        raise ValueError("skill instructions cannot be empty")
    if len(normalized) > MAX_SKILL_CHARS:
        raise ValueError(f"skill instructions exceed {MAX_SKILL_CHARS} characters")
    return normalized


def _matches_type(value: Any, parameter_type: str) -> bool:
    if parameter_type == "string":
        return isinstance(value, str)
    if parameter_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if parameter_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if parameter_type == "boolean":
        return isinstance(value, bool)
    if parameter_type == "array":
        return isinstance(value, list)
    return isinstance(value, dict)


def _validate_value(name: str, value: Any, rule: dict[str, Any]) -> None:
    if not _matches_type(value, rule["type"]):
        raise ValueError(f"parameter {name} must be {rule['type']}")
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError(f"parameter {name} must be one of the configured enum values")


def parse_parameter_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def render_automation_prompt(
    version: AutomationVersion, parameters: dict[str, Any]
) -> str:
    return render_prompt(
        version.prompt_template,
        version.parameter_schema,
        parameters,
    )


def load_definition_file(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"definition file not found: {source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid definition file: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("definition file must contain a JSON object")
    return data


def automation_options(data: dict[str, Any], default_name: str | None = None) -> dict:
    allowed = {
        "format_version",
        "name",
        "description",
        "prompt_template",
        "parameter_schema",
        "workspace",
        "allow_write",
        "allow_command",
        "skills",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("unknown definition fields: " + ", ".join(sorted(unknown)))
    if data.get("format_version", 1) != 1:
        raise ValueError("unsupported definition format_version")
    for field in ("allow_write", "allow_command"):
        if field in data and not isinstance(data[field], bool):
            raise ValueError(f"{field} must be boolean")
    name = data.get("name", default_name)
    if not name:
        raise ValueError("definition requires name")
    skills = data.get("skills", [])
    if not isinstance(skills, list) or not all(
        isinstance(item, str) for item in skills
    ):
        raise ValueError("skills must be an array of skill names")
    return {
        "name": name,
        "description": data.get("description", ""),
        "prompt_template": data.get("prompt_template", ""),
        "parameter_schema": data.get("parameter_schema", {}),
        "workspace": data.get("workspace", "."),
        "allow_write": data.get("allow_write", False) is True,
        "allow_command": data.get("allow_command", False) is True,
        "skill_names": skills,
    }
def validate_parameters(
    schema: dict[str, Any], parameters: dict[str, Any] | None
) -> dict[str, Any]:
    parameters = parameters or {}
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")
    unknown = set(parameters) - set(schema)
    if unknown:
        raise ValueError("unknown parameters: " + ", ".join(sorted(unknown)))
    values: dict[str, Any] = {}
    for name, rule in schema.items():
        if name in parameters:
            value = parameters[name]
        elif "default" in rule:
            value = rule["default"]
        elif rule.get("required"):
            raise ValueError(f"missing required parameter: {name}")
        else:
            continue
        _validate_value(name, value, rule)
        values[name] = value
    return values


def render_prompt(
    template: str, schema: dict[str, Any], parameters: dict[str, Any] | None
) -> str:
    normalized_schema = validate_parameter_schema(schema)
    template = validate_prompt_template(template, normalized_schema)
    values = validate_parameters(normalized_schema, parameters)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"missing value for template parameter: {name}")
        value = values[name]
        return (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False)
        )

    return PLACEHOLDER_PATTERN.sub(replace, template)
