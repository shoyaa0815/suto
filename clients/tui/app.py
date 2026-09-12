import asyncio
from pathlib import Path
from collections.abc import Awaitable, Callable

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import Input, RichLog, Static

from ai import config
from clients.tui.backend import run_session
from clients.tui.output import route_output
from core.modes import get_mode_policy


SUTO_THEME = Theme(
    name="suto-black",
    primary="#3b82f6",
    secondary="#71717a",
    accent="#3b82f6",
    foreground="#e4e4e7",
    background="#000000",
    surface="#000000",
    panel="#000000",
    boost="#000000",
    dark=True,
)

SUTO_WORDMARK = """\
███████ ██   ██ ████████  ██████
██      ██   ██    ██    ██    ██
███████ ██   ██    ██    ██    ██
     ██ ██   ██    ██    ██    ██
███████  █████     ██     ██████"""


class SutoTUI(App[None]):
    """Terminal interface for chat, agent work, and automation commands."""

    CSS_PATH = "suto.tcss"
    TITLE = "Suto"
    SUB_TITLE = ""
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+l", "clear_terminal", "Clear"),
    ]

    def __init__(
        self,
        mode: str,
        session_runner: Callable[[str, Callable[[], Awaitable[str]]], Awaitable[None]] = run_session,
    ) -> None:
        super().__init__()
        self.register_theme(SUTO_THEME)
        self.theme = SUTO_THEME.name
        self.mode = get_mode_policy(mode).name
        self._session_runner = session_runner
        self._prompts: asyncio.Queue[str] = asyncio.Queue()
        self._session_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(id="brand")
            yield RichLog(
                id="terminal",
                markup=False,
                highlight=False,
                wrap=True,
                auto_scroll=True,
            )
            with Horizontal(id="prompt-row"):
                yield Static("›", id="prompt-marker")
                yield Input(
                    id="prompt",
                    placeholder="Ask super suto",
                )
            yield Static(
                f"  {config.AI_MODEL}  {self.mode}   Ctrl+Q exit",
                id="status-bar",
            )

    def on_mount(self) -> None:
        self.screen.styles.background = "#000000"
        self.query_one("#brand", Static).update(
            Text(SUTO_WORDMARK, style="bold #f4f4f5")
        )
        terminal = self.query_one("#terminal", RichLog)
        terminal.write(
            Text.assemble(
                ("directory  ", "#71717a"),
                (str(Path.cwd()), "#d4d4d8"),
            )
        )
        terminal.write("")
        self.query_one("#prompt", Input).focus()
        self._session_task = asyncio.create_task(self._run_session())

    async def _run_session(self) -> None:
        try:
            with route_output(self._write_output):
                await self._session_runner(self.mode, self._prompts.get)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._write_output(f"Suto failed to start: {error}\n")
        else:
            self.exit()

    def _write_output(self, value: str) -> None:
        terminal = self.query_one("#terminal", RichLog)
        for line in value.splitlines() or [""]:
            terminal.write(line)

    @on(Input.Submitted, "#prompt")
    def submit_preview_text(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        terminal = self.query_one("#terminal", RichLog)
        terminal.write(Text.assemble(("› ", "bold #d4d4d8"), value))
        event.input.value = ""
        self._prompts.put_nowait(value)

    async def on_unmount(self) -> None:
        if self._session_task is None or self._session_task.done():
            return
        self._session_task.cancel()
        await asyncio.gather(self._session_task, return_exceptions=True)

    def action_clear_terminal(self) -> None:
        self.query_one("#terminal", RichLog).clear()


def run(mode: str) -> None:
    SutoTUI(mode).run()
