"""Visual-row scrolling for the wrapped CLI chat history."""

from bisect import bisect_right
from collections.abc import Callable
from itertools import accumulate

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import Window, WindowRenderInfo
from prompt_toolkit.layout.controls import UIContent
from prompt_toolkit.layout.margins import Margin


class HistoryScrollbar(Margin):
    def __init__(self, window: "HistoryWindow") -> None:
        self.window = window

    def get_width(self, get_ui_content: Callable[[], UIContent]) -> int:
        return 1

    def create_margin(
        self, window_render_info: WindowRenderInfo, width: int, height: int
    ) -> StyleAndTextTuples:
        if height <= 0:
            return []

        track_height = max(0, height - 2)
        total_rows = max(self.window.total_rows, height)
        thumb_height = (
            max(1, round(track_height * height / total_rows)) if track_height else 0
        )
        thumb_start = (
            round(
                (track_height - thumb_height)
                * self.window.scroll_rows
                / self.window.max_scroll
            )
            if self.window.max_scroll
            else 0
        )
        result: StyleAndTextTuples = [("class:scrollbar.arrow", "^")]
        for row in range(track_height):
            style = (
                "class:scrollbar.button"
                if thumb_start <= row < thumb_start + thumb_height
                else "class:scrollbar.background"
            )
            result.extend([("", "\n"), (style, " ")])
        if height > 1:
            result.extend([("", "\n"), ("class:scrollbar.arrow", "v")])
        return result


class HistoryWindow(Window):
    """Keep a wrapped history viewport independent of its buffer cursor."""

    def __init__(
        self,
        *,
        buffer: Buffer,
        on_scroll: Callable[[], None] | None = None,
        on_follow: Callable[[], None] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.buffer = buffer
        self.on_scroll = on_scroll
        self.on_follow = on_follow
        self.following = True
        self.scroll_rows = 0
        self.total_rows = 0
        self.max_scroll = 0
        self._cached_text: str | None = None
        self._cached_width = 0
        self._line_offsets = [0]
        self.right_margins = [HistoryScrollbar(self)]

    def scroll_by(self, rows: int) -> None:
        self.scroll_rows = max(0, min(self.scroll_rows + rows, self.max_scroll))
        self.following = self.scroll_rows == self.max_scroll
        callback = self.on_follow if self.following else self.on_scroll
        if callback is not None:
            callback()

    def follow_latest(self) -> None:
        self.following = True
        self.scroll_rows = self.max_scroll
        if self.on_follow is not None:
            self.on_follow()

    def _scroll_up(self) -> None:
        self.scroll_by(-3)

    def _scroll_down(self) -> None:
        self.scroll_by(3)

    def _scroll_when_linewrapping(
        self, ui_content: UIContent, width: int, height: int
    ) -> None:
        if width <= 0 or height <= 0:
            self.vertical_scroll = 0
            self.vertical_scroll_2 = 0
            return

        text = self.buffer.text
        if text is not self._cached_text or width != self._cached_width:
            line_heights = (
                max(1, ui_content.get_height_for_line(row, width, self.get_line_prefix))
                for row in range(ui_content.line_count)
            )
            self._line_offsets = [0, *accumulate(line_heights)]
            self._cached_text = text
            self._cached_width = width

        self.total_rows = self._line_offsets[-1]
        self.max_scroll = max(0, self.total_rows - height)
        self.scroll_rows = (
            self.max_scroll if self.following else min(self.scroll_rows, self.max_scroll)
        )
        self.vertical_scroll = max(
            0, bisect_right(self._line_offsets, self.scroll_rows) - 1
        )
        self.vertical_scroll_2 = (
            self.scroll_rows - self._line_offsets[self.vertical_scroll]
        )
