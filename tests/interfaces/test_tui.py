import asyncio

import pytest
from textual.widgets import Input, RichLog, Static

from clients.tui.app import SettingsScreen, SutoTUI
from clients.tui import backend
from clients.tui.output import write
from clients.tui.operations import print_due_reminders, print_pending_reminders
from automation.storage.store import JobStore


async def idle_session(mode, read_prompt):
    await asyncio.Future()


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


async def test_setting_modal_saves_profile_timezone(tmp_path, monkeypatch):
    database_path = tmp_path / "suto.db"
    store = JobStore(database_path)
    user = store.resolve_channel_identity("tui", "local", timezone="UTC")
    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))
    app = SutoTUI("agent", session_runner=idle_session)

    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "/setting"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen)
        timezone = app.screen.query_one("#settings-timezone", Input)
        assert timezone.value == "UTC"
        timezone.value = "Asia/Bangkok"
        await pilot.click("#settings-save")
        await pilot.pause()

        assert not isinstance(app.screen, SettingsScreen)
        assert JobStore(database_path).get_user(user.id).timezone == "Asia/Bangkok"
        lines = app.query_one("#terminal", RichLog).lines
        assert any("Time zone saved: Asia/Bangkok" in line.text for line in lines)


async def test_setting_modal_rejects_unknown_timezone(tmp_path, monkeypatch):
    database_path = tmp_path / "suto.db"
    store = JobStore(database_path)
    user = store.resolve_channel_identity("tui", "local", timezone="UTC")
    monkeypatch.setenv("SUTO_DB_PATH", str(database_path))
    app = SutoTUI("agent", session_runner=idle_session)

    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "/setting"
        await pilot.press("enter")
        await pilot.pause()

        timezone = app.screen.query_one("#settings-timezone", Input)
        timezone.value = "Mars/Olympus"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen)
        error = app.screen.query_one("#settings-error", Static).render()
        assert "unknown timezone" in error.plain
        assert JobStore(database_path).get_user(user.id).timezone == "UTC"


async def test_session_exposes_only_help_noti_and_exit(tmp_path, monkeypatch, capsys):
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
            "/brief at 08:30",
            "/brief status",
            "/brief off",
            "/noti",
            "/noti del resaldfj",
            f"/noti del {reminder.id}",
            "/noti",
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
    assert "สรุปประจำวัน" in output
    assert "Daily briefing scheduled for 08:30" in output
    assert "Daily briefing: 08:30" in output
    assert "Daily briefing disabled." in output
    assert "Pending reminders:" in output
    assert reminder.id in output
    assert "นัดหมอ" in output
    assert "Reminder not found: resaldfj" in output
    assert f"Reminder removed: {reminder.id}" in output
    assert "No pending reminders." in output
    assert store.get_reminder(user.id, reminder.id).status == "cancelled"
    assert "  /help" in output
    assert "  /noti" in output
    assert "  /brief" in output
    assert "  /exit" in output
    assert "bye" in output


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
