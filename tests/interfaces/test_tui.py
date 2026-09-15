import asyncio

import pytest
from textual.containers import Horizontal
from textual.widgets import Input, RichLog, Static

from interfaces.tui.app import SutoTUI
from interfaces.tui import backend
from interfaces.tui.output import write
from interfaces.tui.operations import print_due_reminders, print_pending_reminders
from workflows.storage.store import JobStore


async def idle_session(mode, read_prompt):
    await asyncio.Future()


def test_tui_rejects_home_mode():
    with pytest.raises(ValueError, match="does not use the Textual TUI"):
        SutoTUI("home")


async def test_tui_starts_black_and_focuses_prompt():
    app = SutoTUI("agent", session_runner=idle_session)

    async with app.run_test(size=(100, 30)):
        assert app.screen.styles.background.hex == "#000000"
        prompt = app.query_one("#prompt", Input)
        assert prompt.has_focus
        assert prompt.styles.padding.top == 1
        brand = app.query_one("#brand", Static).render()
        assert brand.plain.startswith("███████")
        assert ">_" not in brand.plain
        status = app.query_one("#status-bar", Static).render()
        assert "agent" in status.plain
        assert "Enter send" not in status.plain


async def test_tui_input_echoes_like_a_terminal():
    received = []

    async def session(mode, read_prompt):
        received.append((mode, await read_prompt()))
        write("suto> handled")
        await asyncio.Future()

    app = SutoTUI("chat", session_runner=session)

    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "hello suto"
        await pilot.press("enter")
        await pilot.pause()

        assert prompt.value == ""
        assert received == [("chat", "hello suto")]
        lines = app.query_one("#terminal", RichLog).lines
        assert any("hello suto" in line.text for line in lines)
        assert any("suto> handled" in line.text for line in lines)


async def test_tui_shows_spinner_while_request_is_running():
    release = asyncio.Event()

    async def session(mode, read_prompt):
        await read_prompt()
        await release.wait()

    app = SutoTUI("agent", session_runner=session)

    async with app.run_test(size=(80, 24)) as pilot:
        activity_row = app.query_one("#activity-row", Horizontal)
        assert activity_row.display is False

        prompt = app.query_one("#prompt", Input)
        prompt.value = "ช่วยคิดหน่อย"
        await pilot.press("enter")
        await pilot.pause()

        assert activity_row.display is True
        assert "suto thinking" in app.query_one(
            "#activity-label", Static
        ).render().plain

        release.set()


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
async def test_tui_runs_the_real_assistant_session_backend(
    mode,
    tmp_path,
    monkeypatch,
):
    calls = []

    async def fake_ask(prompt, **options):
        assert options["mode"] == mode
        assert (options["assistant_context"] is not None) == (mode == "agent")
        calls.append((prompt, options["conversation_history"]))
        return f"answer: {prompt}"

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", fake_ask)
    app = SutoTUI(mode)

    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "hello"
        await pilot.press("enter")

        async with asyncio.timeout(2):
            while not any(
                "suto> answer: hello" in line.text
                for line in app.query_one("#terminal", RichLog).lines
            ):
                await pilot.pause()
        assert app.query_one("#activity-row", Horizontal).display is False

        prompt.value = "/clear"
        await pilot.press("enter")
        async with asyncio.timeout(2):
            while not any(
                "Chat context cleared." in line.text
                for line in app.query_one("#terminal", RichLog).lines
            ):
                await pilot.pause()

        prompt.value = "again"
        await pilot.press("enter")
        async with asyncio.timeout(2):
            while not any(
                "suto> answer: again" in line.text
                for line in app.query_one("#terminal", RichLog).lines
            ):
                await pilot.pause()

        prompt.value = "/reset all"
        await pilot.press("enter")
        async with asyncio.timeout(2):
            while not any(
                "All saved conversations deleted" in line.text
                for line in app.query_one("#terminal", RichLog).lines
            ):
                await pilot.pause()

        prompt.value = "third"
        await pilot.press("enter")
        async with asyncio.timeout(2):
            while not any(
                "suto> answer: third" in line.text
                for line in app.query_one("#terminal", RichLog).lines
            ):
                await pilot.pause()

        assert calls == [
            ("hello", []),
            ("again", []),
            ("third", []),
        ]
