from collections.abc import Callable
from dataclasses import dataclass


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
