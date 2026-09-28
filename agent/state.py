"""Explicit, provider-independent state transitions for a run."""

from dataclasses import dataclass
from enum import StrEnum


class RunState(StrEnum):
    IDLE = "idle"
    PREPARING = "preparing"
    THINKING = "thinking"
    ACTION_REQUESTED = "action_requested"
    AUTHORIZING = "authorizing"
    EXECUTING = "executing"
    OBSERVING = "observing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_INPUT = "waiting_for_input"


_ALLOWED = {
    RunState.IDLE: {RunState.PREPARING, RunState.CANCELLED},
    RunState.PREPARING: {RunState.THINKING, RunState.FAILED, RunState.CANCELLED},
    RunState.THINKING: {RunState.ACTION_REQUESTED, RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.WAITING_FOR_INPUT},
    RunState.ACTION_REQUESTED: {RunState.AUTHORIZING, RunState.FAILED, RunState.CANCELLED, RunState.WAITING_FOR_INPUT},
    RunState.AUTHORIZING: {RunState.EXECUTING, RunState.FAILED, RunState.CANCELLED, RunState.WAITING_FOR_APPROVAL, RunState.WAITING_FOR_INPUT},
    RunState.EXECUTING: {RunState.OBSERVING, RunState.FAILED, RunState.CANCELLED, RunState.WAITING_FOR_APPROVAL},
    RunState.OBSERVING: {RunState.THINKING, RunState.ACTION_REQUESTED, RunState.FAILED, RunState.CANCELLED},
}


@dataclass
class AgentState:
    status: RunState = RunState.IDLE
    iteration: int = 0
    tool_calls: int = 0

    def transition(self, target: RunState) -> None:
        if target not in _ALLOWED.get(self.status, set()):
            raise ValueError(f"invalid agent transition: {self.status} -> {target}")
        self.status = target
