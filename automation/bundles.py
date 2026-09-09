import json
from pathlib import Path
from typing import Any

from .definitions import (
    automation_options,
    load_definition_file,
    validate_name,
    validate_parameter_schema,
    validate_prompt_template,
    validate_skill_instructions,
    validate_workspace,
)
from .redaction import redact_text, redact_value
from .store import JobStore


def export_bundle(store: JobStore, name: str) -> dict[str, Any]:
    automation = store.get_automation(name)
    if automation is None:
        raise ValueError(f"automation not found: {name}")
    version = store.get_current_automation_version(automation.id)
    if version is None:
        raise RuntimeError(f"automation has no current version: {name}")
    skill_versions = store.list_automation_skill_versions(version.id)
    safe_schema = {
        parameter_name: {
            key: redact_value(value) if key == "default" else value
            for key, value in rule.items()
        }
        for parameter_name, rule in version.parameter_schema.items()
    }
    return {
        "format_version": 1,
        "automation": {
            "name": automation.name,
            "description": redact_text(version.description),
            "prompt_template": redact_text(version.prompt_template),
            "parameter_schema": safe_schema,
            "workspace": version.workspace,
            "allow_write": version.allow_write,
            "allow_command": version.allow_command,
            "skills": [skill_name for skill_name, _ in skill_versions],
        },
        "skills": [
            {"name": skill_name, "instructions": redact_text(item.instructions)}
            for skill_name, item in skill_versions
        ],
    }


def write_bundle(store: JobStore, name: str, path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(export_bundle(store, name), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def import_bundle(store: JobStore, path: str | Path) -> tuple[str, int]:
    data = load_definition_file(path)
    unknown_bundle_fields = set(data) - {"format_version", "automation", "skills"}
    if unknown_bundle_fields:
        raise ValueError(
            "unknown bundle fields: " + ", ".join(sorted(unknown_bundle_fields))
        )
    if data.get("format_version") != 1:
        raise ValueError("unsupported definition format_version")
    automation_data = data.get("automation")
    skill_data = data.get("skills", [])
    if not isinstance(automation_data, dict):
        raise ValueError("bundle requires an automation object")
    if not isinstance(skill_data, list):
        raise ValueError("bundle skills must be an array")

    validated_skills = []
    for item in skill_data:
        if not isinstance(item, dict) or set(item) != {"name", "instructions"}:
            raise ValueError("each imported skill requires only name and instructions")
        validated_skills.append(
            (
                validate_name(item["name"], "skill name"),
                redact_text(validate_skill_instructions(item["instructions"])),
            )
        )
    skill_names = [name for name, _ in validated_skills]
    if len(set(skill_names)) != len(skill_names):
        raise ValueError("bundle cannot contain the same skill twice")
    options = automation_options(automation_data)
    options["name"] = validate_name(options["name"], "automation name")
    options["parameter_schema"] = validate_parameter_schema(
        options["parameter_schema"]
    )
    options["prompt_template"] = validate_prompt_template(
        options["prompt_template"], options["parameter_schema"]
    )
    options["workspace"] = validate_workspace(options["workspace"])
    supplied_names = {name for name, _ in validated_skills}
    missing = set(options["skill_names"]) - supplied_names - {
        skill.name for skill in store.list_skills()
    }
    if missing:
        raise ValueError(
            "bundle references missing skills: " + ", ".join(sorted(missing))
        )

    for name, instructions in validated_skills:
        existing = store.get_skill(name)
        if existing is None:
            store.create_skill(name, instructions)
        else:
            current = store.get_current_skill_version(existing.id)
            if current is None or current.instructions != instructions:
                store.revise_skill(name, instructions)
    existing_automation = store.get_automation(options["name"])
    if existing_automation is None:
        automation = store.create_automation(**options)
        return automation.name, automation.current_version
    version = store.revise_automation(**options)
    return options["name"], version.version
