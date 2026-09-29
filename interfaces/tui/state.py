"""Display-only state derived from normalized agent events and conversation turns."""

from dataclasses import dataclass, field

from agent.events import AgentEvent
from workflows.storage.redaction import redact_text


def _safe(value: str, limit: int = 160) -> str:
    """Keep terminal labels short and free of control characters."""
    return redact_text(" ".join(value.split()))[:limit]


@dataclass
class Entry:
    text: str
    kind: str = "info"
    run_id: str | None = None
    parent_run_id: str | None = None
    status: str = ""
    tool_call_id: str | None = None


@dataclass
class UIState:
    session_id: str = ""
    run_status: str = "idle"
    model_status: str = "idle"
    active_skills: tuple[str, ...] = ()
    entries: list[Entry] = field(default_factory=list)
    selected_detail: int | None = None
    _tools: dict[str, Entry] = field(default_factory=dict, repr=False)
    _seen_events: set[str] = field(default_factory=set, repr=False)

    def reset_history(self, session_id: str, messages: list) -> None:
        self.session_id = session_id
        self.entries.clear()
        self._tools.clear()
        self._seen_events.clear()
        self.selected_detail = None
        for message in messages:
            if message.role == "user" or (
                message.role == "assistant" and message.metadata == "{}"
            ):
                self.turn(message.role, message.content)

    def turn(self, role: str, content: str) -> None:
        self.entries.append(Entry(redact_text(content), role))

    def info(self, text: str) -> None:
        self.entries.append(Entry(_safe(text, 400)))

    def detail(self) -> str:
        if self.selected_detail is None or self.selected_detail >= len(self.entries):
            return ""
        item = self.entries[self.selected_detail]
        if item.kind != "tool":
            return ""
        return (
            f"Tool: {item.text} | status: {item.status} | "
            f"run: {item.run_id or '-'} | call: {item.tool_call_id or '-'}"
        )

    def select_previous_tool(self) -> None:
        before = len(self.entries) if self.selected_detail is None else self.selected_detail
        self.selected_detail = next(
            (i for i in range(before - 1, -1, -1) if self.entries[i].kind == "tool"),
            None,
        )

    def apply_event(self, event: AgentEvent) -> None:
        if event.event_id in self._seen_events:
            return
        self._seen_events.add(event.event_id)
        kind = event.type
        child = event.parent_run_id is not None
        if kind.startswith("agent."):
            status = kind.removeprefix("agent.")
            if not child:
                self.run_status = status
            if status in {"preparing", "completed", "failed", "cancelled", "waiting_for_approval"}:
                role = event.data.get("child_role")
                child_name = f"child {role}" if role in {"research", "coding", "review"} else "child"
                label = f"{child_name} {event.run_id[:8]} {status}" if child else f"agent {status}"
                self.entries.append(Entry(label, "event", event.run_id, event.parent_run_id))
        elif kind.startswith("model."):
            status = kind.removeprefix("model.")
            if not child:
                self.model_status = status
            label = f"model {status}"
            if status == "requested":
                iteration = event.data.get("iteration")
                label += f" · round {iteration if type(iteration) is int else '?'}"
            if status in {"requested", "completed", "failed"}:
                self.entries.append(Entry(label, "event", event.run_id, event.parent_run_id))
        elif kind.startswith("tool."):
            call_id = event.tool_call_id
            status = kind.removeprefix("tool.")
            if status == "requested":
                item = Entry("tool", "tool", event.run_id, event.parent_run_id,
                             "requested", call_id)
                self.entries.append(item)
                if call_id:
                    self._tools[call_id] = item
            elif status in {"started", "completed", "failed"}:
                item = self._tools.get(call_id) if call_id else None
                if item is None:
                    item = Entry("tool", "tool", event.run_id, event.parent_run_id,
                                 tool_call_id=call_id)
                    self.entries.append(item)
                    if call_id:
                        self._tools[call_id] = item
                name = event.data.get("tool_name")
                if isinstance(name, str):
                    item.text = _safe(name)
                item.status = status
        elif kind.startswith("permission."):
            status = kind.removeprefix("permission.")
            if status in {"requested", "denied", "approval_required", "approval_requested"}:
                if status in {"denied", "approval_required"} and event.tool_call_id:
                    tool = self._tools.get(event.tool_call_id)
                    if tool is not None:
                        tool.status = "failed" if status == "denied" else "approval"
                self.entries.append(Entry(f"permission {status.replace('_', ' ')}",
                                          "event", event.run_id, event.parent_run_id))
        elif kind.startswith("delegation."):
            status = kind.removeprefix("delegation.")
            if status in {"started", "completed", "failed", "cancelled"}:
                child_id = event.data.get("child_run_id")
                role = event.data.get("child_role")
                label = f"delegation {role} {status}" if role in {"research", "coding", "review"} else f"delegation {status}"
                if isinstance(child_id, str) and child_id.isalnum():
                    label += f" · child {child_id[:8]}"
                self.entries.append(Entry(label, "event", event.run_id, event.parent_run_id))
        elif kind.startswith("context."):
            if kind == "context.compacted":
                self.entries.append(Entry("context compacted", "event", event.run_id,
                                          event.parent_run_id))


def render_entries(state: UIState) -> str:
    """The viewport's plain text; prompt-toolkit handles wrapping and scrolling."""
    lines = []
    for item in state.entries:
        if item.kind in {"user", "assistant"}:
            prefix = "You" if item.kind == "user" else "Suto"
            lines.append(f"{prefix}: {item.text}")
            lines.append("")
            continue
        indent = "    " if item.parent_run_id else "  "
        if item.kind == "tool":
            symbol = {"requested": "○", "started": "●", "completed": "✓",
                      "failed": "✗", "approval": "?"}.get(item.status, "○")
            lines.append(f"{indent}{item.text} {symbol}")
        else:
            lines.append(f"{indent}{item.text}")
    return "\n".join(lines)
