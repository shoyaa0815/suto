"""Terminal adapter behavior without model, MCP, or network access."""

import asyncio
from types import SimpleNamespace

from agent import AgentRequest
from agent.events import AgentEvent
from interfaces.tui.app import TUIView
from interfaces.tui.controller import TUIController
from interfaces.tui.state import UIState, render_entries
from workflows.storage.store import JobStore


def event(kind, *, run="parent", parent=None, call=None, **data):
    return AgentEvent(run, "session", kind, data,
                      parent_run_id=parent, tool_call_id=call)


def controller(tmp_path, monkeypatch, executor):
    monkeypatch.setattr("interfaces.tui.controller.load_settings", lambda: SimpleNamespace(
        profile=SimpleNamespace(display_name="Local", timezone="UTC", locale="en")
    ))
    store = JobStore(tmp_path / "suto.db")
    return TUIController(store=store, executor=executor), store


async def test_tui_starts_and_direct_response_creates_agent_request(tmp_path, monkeypatch):
    seen = []

    async def execute(request, **kwargs):
        seen.append((request, kwargs))
        kwargs["agent_event_callback"](event("agent.preparing"))
        kwargs["agent_event_callback"](event("model.requested", iteration=1))
        kwargs["agent_event_callback"](event("model.completed"))
        kwargs["agent_event_callback"](event("agent.completed"))
        return SimpleNamespace(text="Hello", status="completed")

    ui, store = controller(tmp_path, monkeypatch, execute)
    view = TUIView(ui)
    assert view.application.full_screen
    assert view.application.layout.current_control == view.input.control
    assert ui.submit("Hi")
    await ui.wait_current()
    request, kwargs = seen[0]
    assert isinstance(request, AgentRequest)
    assert request.user_message == "Hi"
    assert request.session_id == ui.state.session_id
    assert store.get_agent_run(request.run_id)["status"] == "completed"
    assert kwargs["assistant_context"].conversation_id == request.session_id
    assert "You: Hi" in view.history.text
    assert "Suto: Hello" in view.history.text
    assert "model requested" in view.history.text
    assert [m.role for m in store.list_messages(request.session_id)] == ["user", "assistant"]


async def test_session_continuation_new_and_scoped_resume(tmp_path, monkeypatch):
    histories = []

    async def execute(request, **kwargs):
        histories.append(kwargs["conversation_history"])
        return SimpleNamespace(text=f"reply {len(histories)}", status="completed")

    ui, store = controller(tmp_path, monkeypatch, execute)
    original = ui.state.session_id
    ui.submit("first")
    await ui.wait_current()
    ui.submit("second")
    await ui.wait_current()
    assert any(message["content"] == "reply 1" for message in histories[1])
    assert ui.state.session_id == original
    ui.submit("/new")
    assert ui.state.session_id != original
    assert "reply 1" not in render_entries(ui.state)
    ui.submit(f"/resume {original}")
    assert ui.state.session_id == original
    assert "reply 1" in render_entries(ui.state)
    other = store.resolve_channel_identity("api", "local")
    foreign = store.get_or_create_conversation(other.id, "api", "local")
    ui.submit(f"/resume {foreign.id}")
    assert ui.state.session_id == original
    assert "Session unavailable" in render_entries(ui.state)


async def test_skills_use_registry_and_flow_into_request(tmp_path, monkeypatch):
    requests = []

    async def execute(request, **kwargs):
        requests.append(request)
        return SimpleNamespace(text="done", status="completed")

    ui, _ = controller(tmp_path, monkeypatch, execute)
    ui.submit("/skills")
    ui.submit("/skill activate research")
    assert ui.state.active_skills == ("research",)
    ui.submit("/skill activate /tmp/SKILL.md")
    assert ui.state.active_skills == ("research",)
    ui.submit("hello")
    await ui.wait_current()
    assert requests[0].active_skills == ("research",)
    ui.submit("/skill deactivate research")
    assert ui.state.active_skills == ()


async def test_skill_selection_restores_in_new_tui_controller(tmp_path, monkeypatch):
    async def execute(request, **kwargs):
        return SimpleNamespace(text="done", status="completed")

    first, store = controller(tmp_path, monkeypatch, execute)
    first.submit("/skill activate research")
    second = TUIController(store=JobStore(store.path), executor=execute)
    assert second.state.active_skills == ("research",)


async def test_missing_persisted_skill_blocks_tui_run(tmp_path, monkeypatch):
    called = []

    async def execute(request, **kwargs):
        called.append(request)
        return SimpleNamespace(text="done", status="completed")

    first, store = controller(tmp_path, monkeypatch, execute)
    store.set_session_skills(first.state.session_id, ("removed_skill",))
    second = TUIController(store=JobStore(store.path), executor=execute)
    second.submit("hello")
    await second.wait_current()
    assert not called
    assert "Selected Skill unavailable" in render_entries(second.state)


async def test_tui_submits_pending_approval_without_deciding_policy(tmp_path, monkeypatch):
    entered = asyncio.Event()
    requests = []

    async def execute(request, **kwargs):
        broker = kwargs["approval_broker"]
        broker.on_request = lambda pending: (requests.append(pending), entered.set())
        pending, answer = await broker.request(request.run_id, f"{request.run_id}:1:1", "test.action")
        return SimpleNamespace(text="done", status="completed" if answer.choice == "allow_once" else "blocked")

    ui, _ = controller(tmp_path, monkeypatch, execute)
    ui.submit("run")
    await entered.wait()
    assert ui.submit(f"/approve {requests[0].id}")
    await ui.wait_current()
    assert ui.state.run_status == "completed"


async def test_cancellation_uses_task_cancellation_and_keeps_draft(tmp_path, monkeypatch):
    entered = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def execute(request, **kwargs):
        async def child():
            try:
                entered.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                raise

        await child()

    ui, store = controller(tmp_path, monkeypatch, execute)
    assert ui.submit("wait")
    await entered.wait()
    assert not ui.submit("draft message")
    assert ui.submit("/cancel")
    await ui.wait_current()
    assert child_cancelled.is_set()
    assert ui.state.run_status == "cancelled"
    assert [m.role for m in store.list_messages(ui.state.session_id)] == ["user"]


def test_event_ordering_tool_failure_permission_and_safe_detail():
    state = UIState()
    sequence = [
        event("agent.preparing"),
        event("model.requested", iteration=1),
        event("model.completed"),
        event("tool.requested", call="call-1", secret="TOKEN"),
        event("permission.requested", call="call-1"),
        event("tool.started", call="call-1", tool_name="file.read", arguments="TOKEN"),
        event("tool.failed", call="call-1", status="failed", error="TOKEN"),
        event("agent.failed"),
    ]
    for item in sequence:
        state.apply_event(item)
    state.apply_event(sequence[-1])
    display = render_entries(state)
    assert display.index("model requested") < display.index("file.read ✗")
    assert display.count("agent failed") == 1
    assert "TOKEN" not in display
    state.select_previous_tool()
    assert "call-1" in state.detail()
    assert "TOKEN" not in state.detail()
    assert state.run_status == "failed"


def test_child_correlation_mcp_tools_and_approval_events():
    state = UIState()
    for item in [
        event("tool.requested", call="delegate"),
        event("tool.started", call="delegate", tool_name="agent.delegate"),
        event("agent.preparing", run="child", parent="parent"),
        event("tool.requested", run="child", parent="parent", call="mcp-call"),
        event("tool.started", run="child", parent="parent", call="mcp-call",
              tool_name="mcp.github.search_code"),
        event("tool.completed", run="child", parent="parent", call="mcp-call"),
        event("agent.completed", run="child", parent="parent"),
        event("delegation.completed", child_run_id="child", status="completed"),
        event("tool.completed", call="delegate"),
        event("tool.requested", call="approval"),
        event("permission.requested", call="approval"),
        event("permission.approval_required", call="approval"),
        event("tool.requested", call="denied"),
        event("permission.denied", call="denied"),
    ]:
        state.apply_event(item)
    display = render_entries(state)
    assert "agent.delegate ✓" in display
    assert "    child child preparing" in display
    assert "    mcp.github.search_code ✓" in display
    assert "delegation completed · child child" in display
    assert "permission approval required" in display
    assert "permission denied" in display
    assert "tool ?" in display
    assert "tool ✗" in display


def test_history_rendering_and_scroll_cursor(tmp_path, monkeypatch):
    async def execute(request, **kwargs):
        return SimpleNamespace(text="done", status="completed")

    ui, _ = controller(tmp_path, monkeypatch, execute)
    view = TUIView(ui)
    for i in range(30):
        ui.state.turn("user", f"message {i}")
    view.refresh()
    assert "message 0" in view.history.text
    assert "message 29" in view.history.text
    view.follow = False
    view.history.buffer.cursor_position = 4
    ui.state.info("new event")
    view.refresh()
    assert view.history.buffer.cursor_position == 4
    assert "new event" in view.history.text


async def test_failure_text_is_sanitized_and_runtime_has_no_tui_dependency(tmp_path, monkeypatch):
    async def execute(request, **kwargs):
        kwargs["agent_event_callback"](event("model.failed", error="secret-token"))
        return SimpleNamespace(text="server password=secret-token", status="failed")

    ui, _ = controller(tmp_path, monkeypatch, execute)
    ui.submit("hello")
    await ui.wait_current()
    display = render_entries(ui.state)
    assert "secret-token" not in display
    assert "Request failed" in display
    async def raised(request, **kwargs):
        raise RuntimeError("password=secret-token")

    other, _ = controller(tmp_path, monkeypatch, raised)
    other.submit("hello")
    await other.wait_current()
    assert "secret-token" not in render_entries(other.state)
    assert other.state.run_status == "failed"
    from agent import runtime
    assert "interfaces.tui" not in runtime.__dict__
