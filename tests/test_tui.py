import asyncio

from textual.widgets import Input, RichLog, Static

from clients.tui.app import SutoTUI
from clients.tui import backend
from clients.tui.output import write


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


async def test_tui_chat_runs_the_real_session_backend(tmp_path, monkeypatch):
    async def fake_ask(prompt, **options):
        assert prompt == "hello"
        assert options["mode"] == "chat"
        return "hello from the model"

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    monkeypatch.setattr(backend, "ask_local_ai", fake_ask)
    app = SutoTUI("chat")

    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "hello"
        await pilot.press("enter")

        async with asyncio.timeout(2):
            while not any(
                "suto> hello from the model" in line.text
                for line in app.query_one("#terminal", RichLog).lines
            ):
                await pilot.pause()
