import builtins
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from rich.text import Text


OutputValue = str | Text
OutputWriter = Callable[[OutputValue], None]
ActivityWriter = Callable[[str | None], None]
_writer: ContextVar[OutputWriter | None] = ContextVar("tui_output_writer", default=None)
_activity_writer: ContextVar[ActivityWriter | None] = ContextVar(
    "tui_activity_writer", default=None
)


def write(*values: object, sep: str = " ", end: str = "\n", **_: object) -> None:
    """Print normally in tests, or route output into the active TUI session."""
    text = sep.join(str(value) for value in values) + end
    writer = _writer.get()
    if writer is None:
        builtins.print(*values, sep=sep, end=end)
    else:
        writer(text)


def write_styled(value: str, style: str) -> None:
    """Write colored text in the TUI and plain text in a regular terminal."""
    writer = _writer.get()
    if writer is None:
        builtins.print(value)
    else:
        writer(Text(value, style=style))


def set_activity(value: str | None) -> None:
    """Update transient TUI activity without adding a permanent log line."""
    writer = _activity_writer.get()
    if writer is not None:
        writer(value)


@contextmanager
def route_output(
    writer: OutputWriter,
    activity_writer: ActivityWriter | None = None,
) -> Iterator[None]:
    token = _writer.set(writer)
    activity_token = _activity_writer.set(activity_writer)
    try:
        yield
    finally:
        _activity_writer.reset(activity_token)
        _writer.reset(token)
