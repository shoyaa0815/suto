import asyncio
from contextlib import nullcontext

import ai
import pytest
from ai.execution.request import prepare_request
from assistant.context import AssistantContext
from prompt_toolkit.application.current import create_app_session, set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input.base import DummyInput
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from interfaces.cli import backend
from interfaces.cli import app as cli_app
from interfaces.cli import commands as cli_commands
from interfaces.cli.commands import CommandContext, handle_command
from interfaces.cli.skill_catalog import load_cli_skills
from interfaces.cli.app import (
    ACTIVITY_ROW_HEIGHT,
    DOT_FRAMES,
    PROMPT_BOX_HEIGHT,
    PromptReader,
    _build_prompt_application,
)
from interfaces.cli.operations import print_due_reminders, print_pending_reminders
from workflows.storage.store import JobStore


def test_plain_cli_starts_session_and_uses_a_simple_prompt(monkeypatch, capsys):
    calls = []

    class FakeReader:
        async def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

        async def __call__(self):
            calls.append("> ")
            return "hello"

        def write(self, value):
            calls.append(("write", value))

        def set_activity(self, value):
            calls.append(("activity", value))

    async def fake_session(mode, read_prompt):
        calls.append((mode, await read_prompt()))

    monkeypatch.setattr(cli_app, "run_session", fake_session)
    monkeypatch.setattr(cli_app, "PromptReader", FakeReader)

    cli_app.run("agent")

    assert capsys.readouterr().out == ""
    assert calls[0] == "start"
    assert any(
        call[0] == "write" and "Suto" in call[1] and "agent" in call[1]
        for call in calls
        if isinstance(call, tuple) and call[0] == "write"
    )
    assert (
        "write", "Type /help for commands. Wheel/PageUp/PageDown: output · End: latest\n\n"
    ) in calls
    assert ("agent", "hello") in calls
    assert calls[-1] == "stop"


def test_cli_prompt_keeps_history_above_a_nonwrapping_bottom_status_row():
    application = _build_prompt_application("> ")

    assert application.full_screen is True
    assert application.mouse_support()
    assert application.erase_when_done is True
    assert isinstance(application.layout.container, HSplit)
    history = application.layout.container.children[0]
    assert application.suto_history_field.window is history
    activity_row = application.layout.container.children[1]
    assert activity_row.height.preferred == ACTIVITY_ROW_HEIGHT == 1
    assert isinstance(activity_row.content, FormattedTextControl)
    assert activity_row.wrap_lines() is False
    gray_box = application.layout.container.children[2]
    assert isinstance(gray_box, HSplit)
    assert gray_box.height.preferred == PROMPT_BOX_HEIGHT == 3
    bound_keys = {
        key
        for binding in application.key_bindings.bindings
        for key in binding.keys
    }
    assert Keys.PageUp in bound_keys
    assert Keys.PageDown in bound_keys
    assert Keys.End in bound_keys


def test_cli_activity_text_is_rendered_from_layout_state():
    reader = PromptReader()
    reader._activity = "Suto is thinking"
    reader._activity_suffix = DOT_FRAMES[2]

    assert reader._activity_text() == "Suto is thinking..."
    reader._activity = None
    assert reader._activity_text() == ""


async def test_wrapped_history_scrolls_with_mouse_and_page_keys():
    with create_app_session(input=DummyInput(), output=DummyOutput()):
        reader = PromptReader()
        application = _build_prompt_application(
            "> ", on_scroll_history=reader._scroll_history,
            on_follow_history=reader._follow_history,
        )
        reader._application = application
        reader.write("".join(f"{index:04d} " for index in range(1000)))
        history = application.suto_history_field
        application.layout.update_parents_relations()

        def top_row():
            application.renderer.render(application, application.layout)
            screen = application.renderer.last_rendered_screen
            return "".join(screen.data_buffer[0][column].char for column in range(20))

        def press(key):
            binding = next(
                item for item in application.key_bindings.bindings
                if item.keys == (key,)
            )
            binding.handler(type("Event", (), {"app": application})())

        try:
            with set_app(application):
                latest = top_row()
                mouse = MouseEvent(
                    position=Point(x=20, y=10),
                    event_type=MouseEventType.SCROLL_UP,
                    button=MouseButton.NONE,
                    modifiers=frozenset(),
                )
                application.renderer.mouse_handlers.mouse_handlers[10][20](mouse)
                after_wheel = top_row()
                assert after_wheel != latest
                assert reader._following_history is False

                press(Keys.PageUp)
                assert top_row() != after_wheel
                press(Keys.PageDown)
                assert top_row() == after_wheel
                assert application.layout.current_window is application.suto_input_field.window

                reader.write("additional output\n")
                assert top_row() == after_wheel
                press(Keys.End)
                assert reader._following_history is True
                assert top_row() != after_wheel
        finally:
            await application.cancel_and_wait_for_background_tasks()


async def test_cli_prompt_reader_queues_submitted_input_without_exiting_application(
    monkeypatch,
):
    reader = PromptReader()

    async def fake_start():
        return None

    monkeypatch.setattr(reader, "start", fake_start)
    pending = asyncio.create_task(reader())
    await asyncio.sleep(0)
    reader._accept("hello")

    assert await pending == "hello"


def test_cli_activity_disables_input_and_ctrl_c_requests_cancellation():
    class FakeField:
        read_only = False

    class FakeApplication:
        suto_input_field = FakeField()

        def invalidate(self):
            pass

    reader = PromptReader()
    reader._application = FakeApplication()

    reader.set_activity("Suto is thinking")

    assert reader._application.suto_input_field.read_only is True
    reader._interrupt()
    assert reader.cancellation_event.is_set()

    reader.set_activity(None)
    assert reader._application.suto_input_field.read_only is False


def test_cli_history_follows_new_output_until_user_scrolls_away():
    class FakeApplication:
        suto_history_field = TextArea(read_only=True)

        def invalidate(self):
            pass

    reader = PromptReader()
    reader._application = FakeApplication()

    reader.write("first\n")
    history = reader._application.suto_history_field.buffer
    assert history.text == "first\n"
    assert history.cursor_position == len(history.text)

    reader._scroll_history()
    history.cursor_position = 1
    reader.write("second\n")

    assert history.text == "first\nsecond\n"
    assert history.cursor_position == 1

    reader._follow_history()
    reader.write("third\n")
    assert history.cursor_position == len(history.text)


def test_cli_user_skill_catalog_skips_invalid_conflicting_and_symlinked_files(
    tmp_path
):
    root = tmp_path / ".suto" / "skills"
    for name, body in (
        ("valid", "name: valid\ndescription: Valid."),
        ("invalid", "name: invalid"),
        ("help", "name: help\ndescription: Reserved."),
        ("coding", "name: coding\ndescription: Duplicate."),
        ("mismatch", "name: elsewhere\ndescription: Mismatched."),
    ):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\n{body}\n---\nInstructions.\n", encoding="utf-8"
        )
    linked = root / "linked"
    linked.symlink_to(root / "valid", target_is_directory=True)

    registry, warnings = load_cli_skills(tmp_path)

    assert {skill.name for skill in registry.list_skills()} == {
        "coding", "research", "valid"
    }
    assert len(warnings) == 5
    assert any("invalid SKILL.md" in warning for warning in warnings)
    assert any("/help is a CLI command" in warning for warning in warnings)
    assert any("name coding already exists" in warning for warning in warnings)
    assert any("symlinks are not supported" in warning for warning in warnings)
    assert any("directory and skill name differ" in warning for warning in warnings)


def test_cli_user_skill_catalog_ignores_missing_and_symlinked_roots(tmp_path):
    registry, warnings = load_cli_skills(tmp_path)
    assert {skill.name for skill in registry.list_skills()} == {"coding", "research"}
    assert warnings == ()

    home_skills = tmp_path / ".suto" / "skills"
    home_skills.parent.mkdir()
    home_skills.symlink_to(tmp_path, target_is_directory=True)
    registry, warnings = load_cli_skills(tmp_path)
    assert {skill.name for skill in registry.list_skills()} == {"coding", "research"}
    assert "symlink" in warnings[0]


def test_cli_ai_request_cannot_use_personal_task_or_reminder_tools(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    conversation = store.get_or_create_conversation(user.id, "tui", "local")
    context = AssistantContext(
        store, user.id, conversation.id, allow_personal_tools=False
    )

    prepared = prepare_request(
        "เตือนให้กินข้าวอีกห้านาที", "agent", None,
        ai.ReplyLanguage("th", "Thai", "test"), "", None, context, None,
    )

    assert "create_reminder_in" not in prepared.allowed_tools
    assert "create_task" not in prepared.allowed_tools
    assert "list_reminders" not in prepared.allowed_tools
    assert "save_memory" in prepared.allowed_tools
    assert "/task and /reminder commands" in prepared.messages[0]["content"]


def test_version_reads_project_metadata_and_handles_missing_file(
    tmp_path, monkeypatch, capsys
):
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text('[project]\nversion = "9.8.7"\n')
    monkeypatch.setattr(cli_commands, "PROJECT_FILE", project_file)
    context = CommandContext(None, None, "conversation", "agent")

    handle_command(context, "/version")
    assert capsys.readouterr().out == "Suto 9.8.7\n"

    monkeypatch.setattr(cli_commands, "PROJECT_FILE", tmp_path / "missing.toml")
    handle_command(context, "/version")
    assert capsys.readouterr().out == "Suto version unavailable.\n"


def test_overdue_reminder_prints_original_time_and_is_not_repeated(
    tmp_path,
    capsys,
):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    store.create_reminder(
        user.id,
        "ประชุมทีม",
        "2020-01-02T09:30:00+07:00",
        timezone="Asia/Bangkok",
        now="2019-01-01T00:00:00+07:00",
    )

    assert print_due_reminders(store, user.id, overdue=True) == 1
    output = capsys.readouterr().out
    assert "เลยเวลาแล้ว" in output
    assert "ประชุมทีม" in output
    assert "2020-01-02 09:30 +07" in output
    assert print_due_reminders(store, user.id, overdue=True) == 0
    assert print_pending_reminders(store, user.id) == 0
    assert "No pending reminders." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("2", "09:00"), ("07:30", "07:30"), ("", None)],
)
async def test_cli_clarification_accepts_number_custom_text_or_cancel(
    typed, expected, monkeypatch, capsys
):
    class FakeApplication:
        async def run_async(self):
            return typed

    def fake_application(prompt):
        assert prompt == "answer> "
        return FakeApplication()

    monkeypatch.setattr(cli_app, "patch_stdout", nullcontext)

    answer = await PromptReader(fake_application).request_clarification(
        {
            "question": "พรุ่งนี้เช้าคือกี่โมง?",
            "options": ["08:00", "09:00"],
        }
    )

    output = capsys.readouterr().out
    assert "Suto needs one detail" in output
    assert "1. 08:00" in output
    assert "2. 09:00" in output
    assert answer == expected


@pytest.mark.parametrize("prompt", [
    "hello", "/clear", "/reset all", "/task new task", "/reminder in 5 minutes",
    "/skills", "/skill activate research", "/research inspect files",
])
async def test_cli_rejects_personal_routes_without_execution_or_data_changes(
    prompt, tmp_path, monkeypatch, capsys,
):
    path = tmp_path / "suto.db"
    monkeypatch.setenv("SUTO_DB_PATH", str(path))
    monkeypatch.delenv("SUTO_NOTIFY_CLI", raising=False)
    monkeypatch.delenv("SUTO_NOTIFY_TUI", raising=False)
    store = JobStore(path)
    user = store.resolve_channel_identity("tui", "local")
    conversation = store.get_or_create_conversation(user.id, "tui", "saved")
    store.add_message(conversation.id, "user", "keep this chat")
    store.save_memory(user.id, "keep this memory")
    task = store.create_task(user.id, "keep this task")
    reminder = store.create_relative_reminder(user.id, "keep this reminder", 5)
    store.save_session_summary(conversation.id, user.id, "keep this summary")
    with store._connect() as db:
        before = list(db.iterdump())

    def forbidden(*args, **kwargs):
        raise AssertionError("Personal or interactive execution must not start")

    for name in ("resolve_channel_identity", "apply_profile_settings", "get_or_create_conversation",
                 "claim_due_reminders", "begin_agent_run", "list_reminders"):
        monkeypatch.setattr(JobStore, name, forbidden)
    monkeypatch.setattr(ai, "ask_local_ai", forbidden)
    monkeypatch.setattr(ai, "execute_local_ai", forbidden)
    import interfaces.cli.operations as operations
    monkeypatch.setattr(operations, "notify_personal_reminders", forbidden)
    import interfaces.cli.skill_catalog as catalog
    monkeypatch.setattr(catalog, "load_cli_skills", forbidden)
    prompts = iter([prompt, "/exit"])

    async def read_prompt():
        return next(prompts)

    await backend.run_session("agent", read_prompt)

    output = capsys.readouterr().out
    assert ("Use /run <task>" if prompt == "hello" else "Unknown command:") in output
    reopened = JobStore(path)
    with reopened._connect() as db:
        assert list(db.iterdump()) == before
    assert reopened.get_task(user.id, task.id) is not None
    assert reopened.get_reminder(user.id, reminder.id) is not None


async def test_cli_submits_job_and_reads_inbox_without_a_chat_session(tmp_path, monkeypatch, capsys):
    path = tmp_path / "suto.db"
    monkeypatch.setenv("SUTO_DB_PATH", str(path))
    monkeypatch.setenv("SUTO_NOTIFY_CLI", "0")
    prompts = iter([f'/run --workspace "{tmp_path}" inspect files', "/jobs", "/notifications", "/exit"])

    async def read_prompt():
        return next(prompts)

    await backend.run_session("agent", read_prompt)

    store = JobStore(path)
    jobs = store.list_jobs()
    assert len(jobs) == 1 and jobs[0].prompt == "inspect files"
    assert jobs[0].status.value == "queued"
    assert jobs[0].allow_write is False and jobs[0].allow_command is False
    with store._connect() as db:
        for table in ("users", "conversations", "agent_runs"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    output = capsys.readouterr().out
    assert "waiting for worker" in output
    assert jobs[0].id in output
    assert "No unread notifications." in output


def test_notifications_command_reads_and_acknowledges_only_requested_item(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    jobs = [store.create_job(f"private prompt {i}") for i in range(2)]
    for job in jobs:
        store.cancel_job(job.id)
    events = store.notifications()
    context = CommandContext(store)
    handle_command(context, "/notifications")
    output = capsys.readouterr().out
    assert all(job.id in output for job in jobs)
    assert "private prompt" not in output
    assert len(JobStore(store.path).notifications()) == 2
    for argument in ("ack", "ack -1", "ack nope", "ack 1 extra", "ack 0", "ack 9223372036854775808", "ack " + "9" * 100):
        handle_command(context, f"/notifications {argument}")
        assert "usage:" in capsys.readouterr().out
        assert len(JobStore(store.path).notifications()) == 2
    handle_command(context, f"/notifications ack {events[0]['id']}")
    assert "Notification acknowledged." in capsys.readouterr().out
    assert [item['id'] for item in JobStore(store.path).notifications()] == [events[1]['id']]
    handle_command(context, f"/notifications ack {events[0]['id']}")
    assert "Unread notification not found." in capsys.readouterr().out


@pytest.mark.parametrize("action", ["", " ack 1"])
def test_notifications_storage_failure_does_not_claim_success(action, tmp_path, monkeypatch, capsys):
    import sqlite3
    store = JobStore(tmp_path / "suto.db")

    def fail(*args):
        raise sqlite3.OperationalError("private internal diagnostic")

    monkeypatch.setattr(store, "notifications", fail)
    monkeypatch.setattr(store, "acknowledge_notification", fail)
    handle_command(CommandContext(store), "/notifications" + action)
    assert capsys.readouterr().out == "Cannot access notifications: storage error.\n"
