"""Immutable plan values. A proposal is not evidence that work was done."""

from dataclasses import dataclass
from typing import Literal


StepStatus = Literal["pending", "running", "completed", "failed", "blocked", "skipped"]
PlanStatus = Literal["proposed", "active", "completed", "failed", "blocked"]


@dataclass(frozen=True)
class PlanStep:
    description: str
    status: StepStatus = "pending"


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: tuple[PlanStep, ...]
    status: PlanStatus = "proposed"
    revision: int = 0
    reason: str | None = None
