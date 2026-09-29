"""Read a small YAML frontmatter plus Markdown instruction body."""

from pathlib import Path

import yaml

from .types import Skill


class SkillLoadError(ValueError):
    pass


_FIELDS = {"name", "description", "recommended_tools", "allowed_tools", "configuration", "metadata"}
_MAX_CHARS = 30_000


class _UniqueKeysLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueKeysLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise SkillLoadError(f"duplicate skill metadata key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_skill(path: str | Path) -> Skill:
    source = Path(path)
    try:
        if source.name != "SKILL.md":
            raise SkillLoadError("skill file must be named SKILL.md")
        content = source.read_text(encoding="utf-8")
        if len(content) > _MAX_CHARS:
            raise SkillLoadError("SKILL.md is too large")
        lines = content.splitlines()
        if len(lines) < 4 or lines[0] != "---" or "---" not in lines[1:]:
            raise SkillLoadError("SKILL.md requires YAML frontmatter and instructions")
        end = lines.index("---", 1)
        metadata = yaml.load("\n".join(lines[1:end]), Loader=_UniqueKeysLoader)
        if not isinstance(metadata, dict) or not all(isinstance(key, str) for key in metadata):
            raise SkillLoadError("skill frontmatter must be a mapping")
        unknown = set(metadata) - _FIELDS
        if unknown:
            raise SkillLoadError(f"unknown skill metadata: {', '.join(sorted(unknown))}")
        for key in ("recommended_tools", "allowed_tools"):
            if key in metadata and (not isinstance(metadata[key], list) or not all(isinstance(item, str) for item in metadata[key])):
                raise SkillLoadError(f"{key} must be a list of tool names")
        skill = Skill(
            name=metadata.get("name"),
            description=metadata.get("description"),
            instructions="\n".join(lines[end + 1:]).strip(),
            recommended_tools=tuple(metadata.get("recommended_tools", [])),
            allowed_tools=(tuple(metadata["allowed_tools"]) if "allowed_tools" in metadata else None),
            configuration=metadata.get("configuration", {}),
            metadata=metadata.get("metadata", {}),
        )
        return skill
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as error:
        if isinstance(error, SkillLoadError):
            raise
        raise SkillLoadError(f"invalid skill file {source}: {error}") from error
