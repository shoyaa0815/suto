from contextlib import nullcontext

import pytest
from prompt_toolkit.layout.containers import HSplit

from interfaces.cli import backend
from interfaces.cli import app as cli_app
from interfaces.cli.app import (
    ACTIVITY_ROW_OFFSET,
    ACTIVITY_ROW_HEIGHT,
    DOT_FRAMES,
    PROMPT_BOX_HEIGHT,
    PROMPT_LAYOUT_HEIGHT,
    PromptReader,
    _build_prompt_application,
)
from interfaces.cli.operations import print_due_reminders, print_pending_reminders
from workflows.storage.store import JobStore


def test_plain_cli_starts_session_and_uses_a_simple_prompt(monkeypatch, capsys):
    calls = []

    class FakeApplication:
        async def run_async(self):
            return "hello"

    def fake_application(prompt):
        calls.append(prompt)
        return FakeApplication()

    async def fake_session(mode, read_prompt):
        calls.append((mode, await read_prompt()))

    monkeypatch.setattr(cli_app, "run_session", fake_session)
    monkeypatch.setattr(cli_app, "_build_prompt_application", fake_application)
    monkeypatch.setattr(cli_app, "patch_stdout", nullcontext)

    cli_app.run("chat")

    output = capsys.readouterr().out
    assert "Suto" in output
    assert "chat" in output
    assert "Type /help for commands." in output
    assert calls[0] == "> "
    assert calls[1] == ("chat", "hello")


def test_cli_prompt_reserves_activity_row_below_the_three_gray_rows():
    application = _build_prompt_application("> ")

    assert application.full_screen is False
    assert application.erase_when_done is False
    assert isinstance(application.layout.container, HSplit)
    dimension = application.layout.container.height
    assert dimension.min == dimension.max == dimension.preferred == PROMPT_LAYOUT_HEIGHT == 6
    gray_box = application.layout.container.children[1]
    assert isinstance(gray_box, HSplit)
    assert gray_box.height.preferred == PROMPT_BOX_HEIGHT == 3
    activity_row = application.layout.container.children[2]
    assert activity_row.height.preferred == ACTIVITY_ROW_HEIGHT == 1
    assert ACTIVITY_ROW_OFFSET == 1


def test_cli_activity_uses_dot_frames_in_the_reserved_row_below_the_prompt(monkeypatch):
    writes = []

    class FakeStdout:
        def isatty(self):
            return True

        def write(self, value):
            writes.append(value)

        def flush(self):
            pass

    monkeypatch.setattr(cli_app.sys, "stdout", FakeStdout())
    reader = PromptReader()
    reader._activity = "Suto is thinking"
    reader._draw_activity(DOT_FRAMES[2])

    assert writes == [
        f"\x1b[{ACTIVITY_ROW_OFFSET}A\r\x1b[2KSuto is thinking...\x1b[{ACTIVITY_ROW_OFFSET}B"
    ]


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
