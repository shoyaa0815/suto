import asyncio
from contextlib import nullcontext

import pytest
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from interfaces.cli import backend
from interfaces.cli import app as cli_app
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

    cli_app.run("chat")

    assert capsys.readouterr().out == ""
    assert calls[0] == "start"
    assert any(
        call[0] == "write" and "Suto" in call[1] and "chat" in call[1]
        for call in calls
        if isinstance(call, tuple) and call[0] == "write"
    )
    assert ("write", "Type /help for commands. PageUp: history · End: latest\n\n") in calls
    assert ("chat", "hello") in calls
    assert calls[-1] == "stop"


def test_cli_prompt_keeps_history_above_a_nonwrapping_bottom_status_row():
    application = _build_prompt_application("> ")

    assert application.full_screen is True
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


def test_cli_activity_text_is_rendered_from_layout_state():
    reader = PromptReader()
    reader._activity = "Suto is thinking"
    reader._activity_suffix = DOT_FRAMES[2]

    assert reader._activity_text() == "Suto is thinking..."
    reader._activity = None
    assert reader._activity_text() == ""


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


async def test_cli_cancels_an_active_request_from_the_persistent_reader(
    tmp_path, monkeypatch, capsys
):
    class Reader:
        def __init__(self):
            self.cancellation_event = asyncio.Event()
            self.prompts = iter(["hello", "/exit"])

        async def __call__(self):
            return next(self.prompts)

        def set_activity(self, value):
            if value is not None:
                self.cancellation_event.set()

    async def slow_ask(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", slow_ask)

    await backend.run_session("chat", Reader())

    assert "request cancelled" in capsys.readouterr().out


async def test_session_exposes_notification_command_and_removes_by_name(
    tmp_path, monkeypatch, capsys
):
    database_path = tmp_path / "suto.db"
    store = JobStore(database_path)
    user = store.resolve_channel_identity("tui", "local")
    reminder = store.create_reminder(
        user.id,
        "นัดหมอ",
        "2099-01-02T09:30:00+07:00",
        timezone="Asia/Bangkok",
    )
    prompts = iter(
        [
            "/jobs",
            "/quit",
            "/brief",
            "/setting",
            "/daily",
            "/noti",
            "/notification",
            "/notification remove ไม่มีชื่อนี้",
            "/notification remove นัดหมอ",
            "/notification",
            "/help",
            "/exit",
        ]
    )

    async def read_prompt():
        return next(prompts)

    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))
    await backend.run_session("agent", read_prompt)

    output = capsys.readouterr().out
    assert "Unknown command: /jobs" in output
    assert "Unknown command: /quit" in output
    assert "Unknown command: /brief" in output
    assert "Unknown command: /setting" in output
    assert "Unknown command: /daily" in output
    assert "Unknown command: /noti" in output
    assert "Pending reminders:" in output
    assert reminder.id in output
    assert "นัดหมอ" in output
    assert "Reminder not found: ไม่มีชื่อนี้" in output
    assert "Reminder removed: นัดหมอ" in output
    assert "No pending reminders." in output
    assert store.get_reminder(user.id, reminder.id).status == "cancelled"
    assert "  /help" in output
    assert "  /notification" in output
    assert "  /notification remove <name>" in output
    assert "  /daily" not in output
    assert "  /exit" in output
    assert "bye" in output


async def test_cli_identity_uses_shared_yaml_profile(tmp_path, monkeypatch):
    database_path = tmp_path / "suto.db"
    store = JobStore(database_path)
    existing = store.resolve_channel_identity(
        "tui",
        "local",
        display_name="Old",
        timezone="UTC",
        locale="en",
    )
    (tmp_path / "config.yaml").write_text(
        "version: 1\nprofile:\n  timezone: Asia/Bangkok\n"
        "  locale: th\n  display_name: Suto Owner\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))

    async def read_prompt():
        return "/exit"

    await backend.run_session("agent", read_prompt)

    user = JobStore(database_path).get_user(existing.id)
    assert user.display_name == "Suto Owner"
    assert user.timezone == "Asia/Bangkok"
    assert user.locale == "th"


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


@pytest.mark.parametrize("mode", ["chat", "agent"])
async def test_cli_runs_the_real_assistant_session_backend(
    mode,
    tmp_path,
    monkeypatch,
    capsys,
):
    calls = []

    async def fake_ask(prompt, **options):
        assert options["mode"] == mode
        assert (options["assistant_context"] is not None) == (mode == "agent")
        calls.append((prompt, options["conversation_history"]))
        return f"answer: {prompt}"

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", fake_ask)
    prompts = iter(["hello", "/clear", "again", "/reset all", "third", "/exit"])

    async def read_prompt():
        return next(prompts)

    await backend.run_session(mode, read_prompt)

    output = capsys.readouterr().out
    assert "answer: hello" in output
    assert "suto>" not in output
    assert "Chat context cleared." in output
    assert "answer: again" in output
    assert "All saved conversations deleted" in output
    assert "answer: third" in output
    assert calls == [
        ("hello", []),
        ("again", []),
        ("third", []),
    ]


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
