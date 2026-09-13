"""Workspace-aware developer tools with a stable package-level API."""

from collections.abc import Callable

from workflows.runtime.context import ExecutionContext

from .reading import build_workspace_read_tools
from .schemas import LIST_SCHEMA, PATCH_SCHEMA, PROMPT, READ_SCHEMA, SEARCH_SCHEMA
from .writing import build_workspace_write_tools


def build_workspace_tools(
    context: ExecutionContext,
    change_callback: Callable[[dict], object] | None = None,
) -> dict[str, object]:
    """Build the request-scoped workspace tool registry."""
    return {
        **build_workspace_read_tools(context),
        **build_workspace_write_tools(context, change_callback),
    }


__all__ = [
    "LIST_SCHEMA",
    "PATCH_SCHEMA",
    "PROMPT",
    "READ_SCHEMA",
    "SEARCH_SCHEMA",
    "build_workspace_tools",
]
