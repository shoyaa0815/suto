"""Skill discovery and session-local activation."""

from functools import lru_cache
from pathlib import Path

from .loader import load_skill
from .types import Skill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def discover(self, root: str | Path) -> tuple[Skill, ...]:
        """Load immediate child skill directories in sorted order."""
        found = tuple(load_skill(path) for path in sorted(Path(root).glob("*/SKILL.md")))
        names = [skill.name for skill in found]
        for skill in found:
            if skill.name in self._skills or names.count(skill.name) != 1:
                raise ValueError(f"skill already registered: {skill.name}")
        for skill in found:
            self.register(skill)
        return found

    def resolve(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as error:
            raise ValueError(f"unknown skill: {name}") from error

    def list_skills(self) -> tuple[Skill, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    def active(self, names: tuple[str, ...]) -> tuple[Skill, ...]:
        if len(set(names)) != len(names):
            raise ValueError("active skills contain duplicates")
        return tuple(self.resolve(name) for name in names)


class SkillSelection:
    """Active names for one interface session; no global activation state."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self._names: list[str] = []

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._names)

    def activate(self, name: str) -> None:
        self.registry.resolve(name)
        if name not in self._names:
            self._names.append(name)

    def deactivate(self, name: str) -> None:
        if name not in self._names:
            raise ValueError(f"skill is not active: {name}")
        self._names.remove(name)


@lru_cache(maxsize=1)
def builtin_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.discover(Path(__file__).parent / "builtin")
    return registry
