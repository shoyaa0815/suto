"""Plain stdin/stdout application for chat and agent modes."""

import asyncio
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.document import Document
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from ai import config
from interfaces.cli.backend import run_session
from interfaces.cli.output import route_output, write as write_output


PROMPT_STYLE = Style.from_dict(
    {
        "input": "bg:#343436 #f4f4f5",
        "prompt": "bold #e5e7eb",
        "placeholder": "#9ca3af",
    }
)

PROMPT_BOX_HEIGHT = 3
ACTIVITY_ROW_HEIGHT = 1
PROMPT_BOX_DIMENSION = Dimension.exact(PROMPT_BOX_HEIGHT)
DOT_FRAMES = (".", "..", "...", "")


def _build_prompt_application(
    prompt: str | Callable[[], str],
    on_accept: Callable[[str], None] | None = None,
    activity: Callable[[], str] | None = None,
    on_interrupt: Callable[[], None] | None = None,
    on_end_of_input: Callable[[], None] | None = None,
    on_scroll_history: Callable[[], None] | None = None,
    on_follow_history: Callable[[], None] | None = None,
) -> Application[str | None]:
    """Build a full-screen chat viewport with input anchored at its bottom."""
    history = TextArea(
        read_only=True,
        focusable=True,
        scrollbar=True,
        wrap_lines=True,
        style="class:history",
    )
    field = TextArea(
        multiline=False,
        prompt=(
            lambda: [("class:prompt", prompt())]
            if callable(prompt)
            else [("class:prompt", prompt)]
        ),
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
        if on_accept is None:
            event.app.exit(result=field.text)
            return
        on_accept(field.text)
        field.buffer.reset()

    @bindings.add("c-c")
    def interrupt(event) -> None:
        if on_interrupt is not None:
            on_interrupt()
            return
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("c-d")
    def end_of_input(event) -> None:
        if on_end_of_input is not None:
            on_end_of_input()
            return
        event.app.exit(exception=EOFError())

    @bindings.add("pageup")
    def scroll_history(event) -> None:
        if on_scroll_history is not None:
            on_scroll_history()
        event.app.layout.focus(history)
        history.buffer.cursor_up(count=10)

    @bindings.add("end")
    def follow_history(event) -> None:
        if on_follow_history is not None:
            on_follow_history()
        history.buffer.cursor_position = len(history.buffer.text)
        event.app.layout.focus(field)

    application = Application(
        layout=Layout(
            HSplit(
                [
                    history,
                    Window(
                        content=FormattedTextControl(
                            activity or (lambda: ""),
                            focusable=False,
                        ),
                        height=Dimension.exact(ACTIVITY_ROW_HEIGHT),
                        wrap_lines=False,
                    ),
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
                ]
            ),
            focused_element=field,
        ),
        key_bindings=bindings,
        style=PROMPT_STYLE,
        full_screen=True,
        erase_when_done=True,
    )
    application.suto_input_field = field
    application.suto_history_field = history
    return application


class PromptReader:
    """Interactive prompt source backed by one persistent application."""

    def __init__(
        self,
        application_factory: Callable[..., Application[str | None]] | None = None,
    ) -> None:
        self.application_factory = application_factory or _build_prompt_application
        self._uses_persistent_application = application_factory is None
        self._activity: str | None = None
        self._activity_suffix = ""
        self._spinner_task: asyncio.Task[None] | None = None
        self._requests: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.cancellation_event = asyncio.Event()
        self._application: Application[str | None] | None = None
        self._application_task: asyncio.Task[str | None] | None = None
        self._prompt = "> "
        self._following_history = True

    def _activity_text(self) -> str:
        """Return one logical status line; the activity window never wraps it."""
        if self._activity is None:
            return ""
        return f"{self._activity}{self._activity_suffix}"

    def _invalidate(self) -> None:
        if self._application is not None:
            self._application.invalidate()

    def _accept(self, value: str) -> None:
        if value:
            self.write(f"{self._prompt}{value}\n")
        self._requests.put_nowait(value)

    def _scroll_history(self) -> None:
        self._following_history = False

    def _follow_history(self) -> None:
        self._following_history = True

    def write(self, value: object) -> None:
        """Append CLI output to the scrollable chat history."""
        if self._application is None:
            return
        history = getattr(self._application, "suto_history_field", None)
        if history is None:
            return
        previous = history.buffer.document
        text = previous.text + str(value)
        cursor_position = (
            len(text) if self._following_history else previous.cursor_position
        )
        history.buffer.set_document(
            Document(text, cursor_position), bypass_readonly=True
        )
        self._invalidate()

    def _interrupt(self) -> None:
        if self._activity is None:
            self._requests.put_nowait(KeyboardInterrupt())
        else:
            self.cancellation_event.set()

    def _end_of_input(self) -> None:
        self._requests.put_nowait(EOFError())

    async def start(self) -> None:
        """Start the application once, keeping it alive between requests."""
        if self._application_task is not None:
            return
        self._application = self.application_factory(
            lambda: self._prompt,
            self._accept,
            self._activity_text,
            self._interrupt,
            self._end_of_input,
            self._scroll_history,
            self._follow_history,
        )
        self._application_task = asyncio.create_task(self._application.run_async())
        await asyncio.sleep(0)

    async def stop(self) -> None:
        """Stop the persistent application and its transient spinner."""
        if self._spinner_task is not None:
            self._spinner_task.cancel()
            self._spinner_task = None
        if self._application is not None and self._application_task is not None:
            if not self._application_task.done():
                self._application.exit()
            await self._application_task
        self._application = None
        self._application_task = None

    async def _spin_activity(self) -> None:
        frame = 0
        try:
            while self._activity is not None:
                self._activity_suffix = DOT_FRAMES[frame % len(DOT_FRAMES)]
                self._invalidate()
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
        self._activity_suffix = ""
        if self._application is not None:
            input_field = getattr(self._application, "suto_input_field", None)
            if input_field is not None:
                input_field.read_only = value is not None
        if value is None:
            self._invalidate()
        elif self._application_task is not None:
            self._spinner_task = asyncio.create_task(self._spin_activity())
        self._invalidate()

    async def _read(self, prompt: str) -> str:
        # Retain the injectable one-shot factory used by non-interactive tests.
        if not self._uses_persistent_application:
            application = self.application_factory(prompt)
            with patch_stdout():
                result = await application.run_async()
            return "" if result is None else result
        await self.start()
        self._prompt = prompt
        self._invalidate()
        result = await self._requests.get()
        if isinstance(result, BaseException):
            raise result
        return result

    async def __call__(self) -> str:
        return await self._read("> ")

    async def request_clarification(
        self, request: dict[str, list[str] | str]
    ) -> str | None:
        options = request["options"]
        assert isinstance(options, list)
        write_output(f"\nSuto needs one detail: {request['question']}")
        for index, option in enumerate(options, start=1):
            write_output(f"  {index}. {option}")
        answer = (await self._read("answer> ")).strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer


def run(mode: str) -> None:
    """Run one plain terminal session."""
    async def run_prompt_session() -> None:
        reader = PromptReader()
        with route_output(reader.write, reader.set_activity), patch_stdout():
            await reader.start()
            reader.write(f"Suto · {config.AI_MODEL} · {mode}\n")
            reader.write("Type /help for commands. PageUp: history · End: latest\n\n")
            try:
                await run_session(mode, reader)
            finally:
                await reader.stop()

    asyncio.run(run_prompt_session())
