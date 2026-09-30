"""Combine built-in and user-owned skills for the CLI."""

from pathlib import Path

from skills import SkillLoadError, SkillRegistry, builtin_registry, load_skill

from .commands import COMMAND_HANDLERS


def load_cli_skills(home: Path | None = None) -> tuple[SkillRegistry, tuple[str, ...]]:
    registry = SkillRegistry()
    for skill in builtin_registry().list_skills():
        registry.register(skill)

    root = (home or Path.home()) / ".suto" / "skills"
    if root.is_symlink() or root.parent.is_symlink():
        return registry, ("Skipped user skills: ~/.suto/skills is a symlink.",)
    if not root.exists():
        return registry, ()
    if not root.is_dir():
        return registry, ("Skipped user skills: ~/.suto/skills is not a directory.",)

    warnings: list[str] = []
    reserved = {command.removeprefix("/") for command in COMMAND_HANDLERS}
    for path in sorted(root.glob("*/SKILL.md")):
        label = path.parent.name
        if path.parent.is_symlink() or path.is_symlink():
            warnings.append(f"Skipped user skill {label}: symlinks are not supported.")
            continue
        try:
            skill = load_skill(path)
        except SkillLoadError:
            warnings.append(f"Skipped user skill {label}: invalid SKILL.md.")
            continue
        if skill.name != label:
            warnings.append(f"Skipped user skill {label}: directory and skill name differ.")
            continue
        if skill.name in reserved:
            warnings.append(f"Skipped user skill {label}: /{skill.name} is a CLI command.")
            continue
        try:
            registry.register(skill)
        except ValueError:
            warnings.append(f"Skipped user skill {label}: name {skill.name} already exists.")
    return registry, tuple(warnings)
