from typing import Protocol

from automation.runtime.context import ExecutionContext
from automation.models import JobStep, StepStatus


MAX_PLAN_STEPS = 20
MAX_STEP_CHARS = 500
MAX_RESULT_CHARS = 4_000

PLANNING_TOOL_NAMES = frozenset({"create_plan", "update_step", "revise_plan"})

PROMPT = """- For every automation job that needs more than one action, call
  create_plan before acting. Keep the plan concrete and ordered.
- Mark a step in_progress before working on it, then completed with a short
  evidence-based result. Mark it failed only when the action actually failed.
- Call revise_plan when new information changes the approach. The revised plan
  replaces the current plan, so include every remaining step in the new plan.
- A resumed job includes its durable checkpoint in the user prompt. Continue
  pending steps and do not recreate or repeat completed plan steps.
- Planning tools record durable job state. Do not claim a plan or step update
  unless the corresponding tool call succeeded."""

CREATE_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_plan",
        "description": "Create the durable ordered plan for the current job.",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_PLAN_STEPS,
                    "description": "Concrete ordered steps needed to finish the job.",
                }
            },
            "required": ["steps"],
        },
    },
}

UPDATE_STEP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_step",
        "description": "Update the status and result of one current plan step.",
        "parameters": {
            "type": "object",
            "properties": {
                "step_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "One-based step number shown in the plan.",
                },
                "status": {
                    "type": "string",
                    "enum": [item.value for item in StepStatus],
                },
                "result": {
                    "type": "string",
                    "description": "Short result or evidence for this status update.",
                },
            },
            "required": ["step_number", "status"],
        },
    },
}

REVISE_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "revise_plan",
        "description": "Replace the current plan when new information changes it.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the current plan must change.",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_PLAN_STEPS,
                    "description": "Complete replacement plan in execution order.",
                },
            },
            "required": ["reason", "steps"],
        },
    },
}


class PlanStore(Protocol):
    def create_plan(self, job_id: str, descriptions: list[str]) -> list[JobStep]: ...

    def revise_plan(self, job_id: str, descriptions: list[str]) -> list[JobStep]: ...

    def update_step(
        self,
        job_id: str,
        position: int,
        status: str | StepStatus,
        result: str | None = None,
    ) -> JobStep: ...


def _validate_steps(steps: list[str]) -> list[str]:
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")
    if len(steps) > MAX_PLAN_STEPS:
        raise ValueError(f"a plan cannot contain more than {MAX_PLAN_STEPS} steps")
    normalized = [" ".join(str(step).split()) for step in steps]
    if any(not step for step in normalized):
        raise ValueError("plan steps cannot be empty")
    if any(len(step) > MAX_STEP_CHARS for step in normalized):
        raise ValueError(f"each plan step is limited to {MAX_STEP_CHARS} characters")
    return normalized


def _format_plan(steps: list[JobStep]) -> str:
    return "\n".join(
        f"{step.position}. [{step.status.value}] {step.description}"
        for step in steps
    )


def build_planning_tools(
    context: ExecutionContext,
    store: PlanStore,
) -> dict[str, object]:
    def create_plan(steps: list[str]) -> str:
        context.require_tool("create_plan")
        created = store.create_plan(context.job_id, _validate_steps(steps))
        return "created job plan:\n" + _format_plan(created)

    def update_step(
        step_number: int,
        status: str,
        result: str | None = None,
    ) -> str:
        context.require_tool("update_step")
        if result is not None and len(result) > MAX_RESULT_CHARS:
            raise ValueError(
                f"step result is limited to {MAX_RESULT_CHARS} characters"
            )
        step = store.update_step(
            context.job_id,
            int(step_number),
            status,
            result,
        )
        suffix = f" — {step.result}" if step.result else ""
        return (
            f"updated step {step.position}: [{step.status.value}] "
            f"{step.description}{suffix}"
        )

    def revise_plan(reason: str, steps: list[str]) -> str:
        context.require_tool("revise_plan")
        compact_reason = " ".join(str(reason).split())
        if not compact_reason:
            raise ValueError("a revision reason is required")
        revised = store.revise_plan(context.job_id, _validate_steps(steps))
        return f"revised job plan ({compact_reason}):\n" + _format_plan(revised)

    return {
        "create_plan": create_plan,
        "update_step": update_step,
        "revise_plan": revise_plan,
    }
