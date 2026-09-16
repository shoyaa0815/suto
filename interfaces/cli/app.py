"""Plain stdin/stdout application for chat and agent modes."""

import asyncio
import sys
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from ai import config
from interfaces.cli.backend import run_session


PROMPT_STYLE = Style.from_dict(
    {
        "input": "bg:#343436 #f4f4f5",
        "prompt": "bold #e5e7eb",
        "placeholder": "#9ca3af",
    }
)

PROMPT_BOX_HEIGHT = 3
PROMPT_BOX_MARGIN_HEIGHT = 1
PROMPT_LAYOUT_HEIGHT = PROMPT_BOX_HEIGHT + (PROMPT_BOX_MARGIN_HEIGHT * 2)
PROMPT_BOX_DIMENSION = Dimension.exact(PROMPT_BOX_HEIGHT)
PROMPT_LAYOUT_DIMENSION = Dimension.exact(PROMPT_LAYOUT_HEIGHT)
ACTIVITY_ROW_OFFSET = PROMPT_LAYOUT_HEIGHT - 1
DOT_FRAMES = (".", "..", "...", "")


def _build_prompt_application(prompt: str) -> Application[str]:
    """Build a gray input area with one terminal-colored row above and below."""
    field = TextArea(
        multiline=False,
        prompt=[("class:prompt", prompt)],
        height=Dimension.exact(1),
        style="bg:#343436 #f4f4f5",
    )
    field.control.input_processors.append(
        ConditionalProcessor(
            AfterInput("Ask super suto", style="class:placeholder"),
            filter=Condition(lambda: not field.text),
        )
    )
    bindings = KeyBindings()

    @bindings.add("enter")
    def accept(event) -> None:
        event.app.exit(result=field.text)

    @bindings.add("c-c")
    def interrupt(event) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("c-d")
    def end_of_input(event) -> None:
        event.app.exit(exception=EOFError())

    return Application(
        layout=Layout(
            HSplit(
                [
                    Window(height=Dimension.exact(PROMPT_BOX_MARGIN_HEIGHT), char=" "),
                    HSplit(
                        [
                            Window(
                                height=Dimension.exact(1),
                                char=" ",
                                style="bg:#343436",
                            ),
                            field,
                            Window(
                                height=Dimension.exact(1),
                                char=" ",
                                style="bg:#343436",
                            ),
                        ],
                        height=PROMPT_BOX_DIMENSION,
                    ),
                    Window(height=Dimension.exact(PROMPT_BOX_MARGIN_HEIGHT), char=" "),
                ],
                height=PROMPT_LAYOUT_DIMENSION,
            ),
            focused_element=field,
        ),
        key_bindings=bindings,
        style=PROMPT_STYLE,
        full_screen=False,
        erase_when_done=False,
    )


class PromptReader:
    """Interactive prompt source with an inline clarification flow."""

    def __init__(
        self,
        application_factory: Callable[[str], Application[str]] | None = None,
    ) -> None:
        self.application_factory = application_factory or _build_prompt_application
        self._activity: str | None = None
        self._spinner_task: asyncio.Task[None] | None = None

    def _draw_activity(self, suffix: str) -> None:
        """Replace the reserved row above the retained prompt without scrolling."""
        if not sys.stdout.isatty():
            return
        label = "" if self._activity is None else f"{self._activity}{suffix}"
        sys.stdout.write(
            f"\x1b[{ACTIVITY_ROW_OFFSET}A\r\x1b[2K{label}\x1b[{ACTIVITY_ROW_OFFSET}B"
        )
        sys.stdout.flush()

    async def _spin_activity(self) -> None:
        frame = 0
        try:
            while self._activity is not None:
                self._draw_activity(DOT_FRAMES[frame % len(DOT_FRAMES)])
                frame += 1
                await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            pass

    def set_activity(self, value: str | None) -> None:
        """Show or clear a compact animated status above the input area."""
        if self._spinner_task is not None:
            self._spinner_task.cancel()
            self._spinner_task = None
        self._activity = value
        if value is None:
            self._draw_activity("")
        elif sys.stdout.isatty():
            self._spinner_task = asyncio.create_task(self._spin_activity())

    async def _read(self, prompt: str) -> str:
        application = self.application_factory(prompt)
        with patch_stdout():
            return await application.run_async()

    async def __call__(self) -> str:
        return await self._read("> ")

    async def request_clarification(
        self, request: dict[str, list[str] | str]
    ) -> str | None:
        options = request["options"]
        assert isinstance(options, list)
        print(f"\nSuto needs one detail: {request['question']}")
        for index, option in enumerate(options, start=1):
            print(f"  {index}. {option}")
        answer = (await self._read("answer> ")).strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer


def run(mode: str) -> None:
    """Run one plain terminal session."""
    print(f"Suto · {config.AI_MODEL} · {mode}")
    print("Type /help for commands.\n")
    asyncio.run(run_session(mode, PromptReader()))
