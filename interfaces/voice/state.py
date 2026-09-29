"""Interface display state derived from the existing runtime events."""

from dataclasses import dataclass, field

from agent.events import AgentEvent


@dataclass
class VoiceState:
    session_id: str = ""
    run_id: str | None = None
    status: str = "idle"
    transcript: str = ""
    display_text: str = ""
    spoken_text: str = ""
    active_skills: tuple[str, ...] = ()
    pending_approval_id: str | None = None
    tool_name: str | None = None
    events: list[AgentEvent] = field(default_factory=list)

    def apply_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        if event.parent_run_id is not None:
            return
        if event.type.startswith("model."):
            self.status = "thinking"
        elif event.type.startswith("tool."):
            self.status = "tool_activity"
            name = event.data.get("tool_name")
            if isinstance(name, str):
                self.tool_name = name
        elif event.type == "permission.approval_requested":
            self.status = "waiting_approval"
        elif event.type == "agent.cancelled":
            self.status = "cancelled"
