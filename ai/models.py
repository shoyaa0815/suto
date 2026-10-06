from collections.abc import Callable
from dataclasses import dataclass

from planning.models import Plan


ProgressCallback = Callable[[dict], object]
ToolEventCallback = Callable[[dict], object]
ChangeEventCallback = Callable[[dict], object]


@dataclass(frozen=True)
class AIExecutionResult:
    text: str
    status: str
    error: str | None
    prompt_tokens: int
    output_tokens: int
    elapsed_seconds: float
    clarification: dict[str, list[str] | str] | None = None
    plan: Plan | None = None
    error_code: str | None = None
