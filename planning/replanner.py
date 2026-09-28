"""Revise proposed steps without erasing verified completed work."""

from dataclasses import replace

from .models import Plan
from .planner import MAX_STEPS, validate_steps


class Replanner:
    def revise(self, plan: Plan, remaining_steps: list[str], reason: str) -> Plan:
        reason = " ".join(reason.split())
        if not reason:
            raise ValueError("a revision reason is required")
        history = tuple(
            step for step in plan.steps if step.status in {"completed", "failed", "skipped"}
        )
        revised = validate_steps(remaining_steps, max_steps=MAX_STEPS - len(history))
        return replace(
            plan,
            steps=(*history, *revised),
            status="proposed",
            revision=plan.revision + 1,
            reason=reason,
        )

    def block(self, plan: Plan, reason: str) -> Plan:
        reason = " ".join(reason.split())
        if not reason:
            raise ValueError("a blocking reason is required")
        return replace(plan, status="blocked", reason=reason)
