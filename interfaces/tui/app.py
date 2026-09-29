"""Full-screen prompt-toolkit view over the TUI controller and event state."""

import asyncio

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from ai import config

from .controller import TUIController
from .state import render_entries


STYLE = Style.from_dict({
    "status": "bg:#242435 #d4d4d8",
    "help": "bg:#242435 #a1a1aa",
    "input": "bg:#343436 #f4f4f5",
    "history": "#f4f4f5",
})


class TUIView:
    def __init__(self, controller: TUIController) -> None:
        self.controller = controller
        self.follow = True
        self.history = TextArea(read_only=True, focusable=True, scrollbar=True,
                                wrap_lines=True, style="class:history")
        self.input = TextArea(multiline=False, prompt="> ", height=Dimension.exact(1),
                              style="class:input")
        bindings = KeyBindings()

        @bindings.add("enter")
        def accept(event) -> None:
            if self.controller.submit(self.input.text):
                self.input.buffer.reset()
            if self.controller.exit_requested:
                event.app.exit()

        @bindings.add("c-c")
        def cancel(event) -> None:
            if self.controller.busy:
                self.controller.cancel()
            else:
                event.app.exit()

        @bindings.add("c-d")
        def exit_ui(event) -> None:
            event.app.exit()

        @bindings.add("pageup")
        def page_up(event) -> None:
            self.follow = False
            event.app.layout.focus(self.history)
            self.history.buffer.cursor_up(count=10)

        @bindings.add("pagedown")
        def page_down(event) -> None:
            self.history.buffer.cursor_down(count=10)

        @bindings.add("end")
        def latest(event) -> None:
            self.follow = True
            self.history.buffer.cursor_position = len(self.history.text)
            event.app.layout.focus(self.input)

        @bindings.add("escape")
        def focus_input(event) -> None:
            event.app.layout.focus(self.input)

        @bindings.add("f2")
        def tool_detail(event) -> None:
            self.controller.state.select_previous_tool()
            event.app.invalidate()

        state = self.controller.state
        self.application = Application(
            layout=Layout(HSplit([
                Window(FormattedTextControl(
                    lambda: f" Suto TUI · session {state.session_id} · skills {', '.join(state.active_skills) or 'none'}"
                ), height=1, style="class:status"),
                self.history,
                Window(FormattedTextControl(
                    lambda: f" Run {state.run_status} · model {config.AI_MODEL}: {state.model_status}"
                ), height=1, style="class:status"),
                Window(FormattedTextControl(lambda: " " + (state.detail() or
                    "PageUp history · End latest/input · F2 tool detail · Ctrl-C cancel · /help")),
                    height=1, style="class:help"),
                self.input,
            ]), focused_element=self.input),
            key_bindings=bindings, style=STYLE, full_screen=True, erase_when_done=True,
        )
        self.controller.on_change = self.refresh
        self.refresh()

    def refresh(self) -> None:
        text = render_entries(self.controller.state)
        position = len(text) if self.follow else min(
            self.history.buffer.cursor_position, len(text)
        )
        self.history.buffer.set_document(
            Document(text, cursor_position=position), bypass_readonly=True
        )
        self.application.invalidate()


def run(mode: str = "agent") -> None:
    if mode != "agent":
        raise ValueError("TUI supports agent mode only")

    async def main() -> None:
        controller = TUIController()
        view = TUIView(controller)
        try:
            await view.application.run_async()
        finally:
            await controller.close()

    asyncio.run(main())
