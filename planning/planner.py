"""Select explicit multi-action requests and ask the configured model for a plan."""

import json
import re

from llm.base import Model
from llm.types import ModelRequest, ModelResponse

from .models import Plan, PlanStep


MAX_STEPS = 8
MAX_STEP_CHARS = 300

_ENGLISH_ACTIONS = re.compile(
    r"\b(?:inspect|investigate|find|fix|implement|build|create|update|refactor|"
    r"run|test|verify|deploy|analyze|review|write|search|summarize)\b",
    re.IGNORECASE,
)
_THAI_ACTIONS = re.compile(
    r"ตรวจ|แก้|สร้าง|เขียน|ทดสอบ|รัน|วิเคราะห์|ปรับ|ติดตั้ง|ค้น|สรุป"
)
_SEQUENCE = re.compile(r"\b(?:and|then|before|after)\b|,|;|\n|แล้ว|จากนั้น|และ", re.IGNORECASE)
_NUMBERED_STEP = re.compile(r"(?m)^\s*\d+[.)]\s+\S")

_PLANNING_PROMPT = (
    "Propose a short execution plan for the user's multi-step request. "
    "Return ONLY a JSON object with a 'steps' array of 2 to 8 concrete strings, "
    "in execution order. Each string must be at most 300 characters. "
    "Include only actions needed for the request. Do not claim any action is done. "
    "The plan gives guidance and cannot expand tool permissions."
)


def validate_steps(
    steps: object, *, min_steps: int = 1, max_steps: int = MAX_STEPS
) -> tuple[PlanStep, ...]:
    if not isinstance(steps, list) or not min_steps <= len(steps) <= max_steps:
        raise ValueError(f"plan must contain {min_steps} to {max_steps} steps")
    normalized = []
    for step in steps:
        if not isinstance(step, str):
            raise ValueError("plan steps must be strings")
        description = " ".join(step.split())
        if not description or len(description) > MAX_STEP_CHARS:
            raise ValueError("plan step is empty or too long")
        normalized.append(PlanStep(description))
    return tuple(normalized)


class Planner:
    def __init__(self, model: Model) -> None:
        self.model = model

    @staticmethod
    def needs_plan(prompt: str) -> bool:
        if re.match(r"^\s*(?:what|why|how|when|where|who)\b", prompt, re.IGNORECASE):
            return False
        if len(_NUMBERED_STEP.findall(prompt)) >= 2:
            return True
        actions = [*list(_ENGLISH_ACTIONS.finditer(prompt)), *list(_THAI_ACTIONS.finditer(prompt))]
        actions.sort(key=lambda match: match.start())
        if len(actions) < 2:
            return False
        return bool(_SEQUENCE.search(prompt[actions[0].end():actions[-1].start()]))

    @staticmethod
    def request(prompt: str) -> ModelRequest:
        return ModelRequest(
            messages=[
                {"role": "system", "content": _PLANNING_PROMPT},
                {"role": "user", "content": prompt},
            ],
            available_tools=[],
        )

    @staticmethod
    def parse(prompt: str, response: ModelResponse) -> Plan:
        if response.tool_calls or not response.text or len(response.text) > 8192:
            raise ValueError("model did not return a bounded text plan")
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as error:
            raise ValueError("model returned invalid plan JSON") from error
        if not isinstance(data, dict):
            raise ValueError("model returned invalid plan JSON")
        return Plan(goal=prompt, steps=validate_steps(data.get("steps"), min_steps=2))

    async def create(self, prompt: str) -> Plan:
        return self.parse(prompt, await self.model.generate(self.request(prompt)))
