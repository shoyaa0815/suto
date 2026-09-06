# This package is the single place that "controls" every tool the AI can
# call. Each tool lives in its own module (datetime_tool.py, search.py,
# fetch.py, ...) and exports three things:
#   - a handler function (e.g. search_web)       -> what the tool does
#   - SCHEMA                                      -> how Ollama calls it
#   - PROMPT                                       -> when the AI should call it
#
# To add a new tool: create tools/your_tool.py with those three things,
# then add it to the imports and the three dicts/lists below. Nothing
# outside this package needs to change. The AI executor asks this registry for tools
# allowed by the active mode instead of exposing every tool globally.
from .datetime_tool import get_current_datetime
from .datetime_tool import PROMPT as DATETIME_PROMPT
from .datetime_tool import SCHEMA as DATETIME_SCHEMA
from .command import build_command_tools
from .command import COMMAND_TOOL_NAMES
from .command import PROMPT as COMMAND_PROMPT
from .command import SCHEMA as COMMAND_SCHEMA
from .fetch import fetch_url
from .fetch import PROMPT as FETCH_PROMPT
from .fetch import SCHEMA as FETCH_SCHEMA
from .file_reader import build_attachment_tools
from .file_reader import PROMPT as FILE_READER_PROMPT
from .file_reader import READ_SCHEMA as FILE_READER_SCHEMA
from .file_reader import SEARCH_SCHEMA as FILE_SEARCH_SCHEMA
from .file_reader import SUMMARY_SCHEMA as FILE_SUMMARY_SCHEMA
from .search import search_web
from .search import PROMPT as SEARCH_PROMPT
from .search import SCHEMA as SEARCH_SCHEMA
from .planning import build_planning_tools
from .planning import CREATE_PLAN_SCHEMA
from .planning import PLANNING_TOOL_NAMES
from .planning import PROMPT as PLANNING_PROMPT
from .planning import REVISE_PLAN_SCHEMA, UPDATE_STEP_SCHEMA
from .workspace import build_workspace_tools
from .workspace import PATCH_SCHEMA as WORKSPACE_PATCH_SCHEMA
from .workspace import LIST_SCHEMA as WORKSPACE_LIST_SCHEMA
from .workspace import PROMPT as WORKSPACE_PROMPT
from .workspace import READ_SCHEMA as WORKSPACE_READ_SCHEMA
from .workspace import SEARCH_SCHEMA as WORKSPACE_SEARCH_SCHEMA

_EMPTY_ATTACHMENT_TOOLS = build_attachment_tools({})


def _workspace_unavailable(**kwargs) -> str:
    return "workspace tools are unavailable outside an automation job"

ALL_TOOLS = {
    "get_current_datetime": get_current_datetime,
    "search_web": search_web,
    "fetch_url": fetch_url,
    # This placeholder is replaced with a request-scoped handler by ai/executor.py.
    "read_attached_file": _EMPTY_ATTACHMENT_TOOLS["read_attached_file"],
    "search_attachment": _EMPTY_ATTACHMENT_TOOLS["search_attachment"],
    "summarize_attachment": _EMPTY_ATTACHMENT_TOOLS["summarize_attachment"],
    "list_workspace_files": _workspace_unavailable,
    "read_workspace_file": _workspace_unavailable,
    "search_workspace": _workspace_unavailable,
    "apply_workspace_patch": _workspace_unavailable,
    "create_plan": _workspace_unavailable,
    "update_step": _workspace_unavailable,
    "revise_plan": _workspace_unavailable,
    "run_workspace_command": _workspace_unavailable,
}

ALL_TOOL_SCHEMAS = {
    schema["function"]["name"]: schema
    for schema in [
        DATETIME_SCHEMA,
        SEARCH_SCHEMA,
        FETCH_SCHEMA,
        FILE_READER_SCHEMA,
        FILE_SEARCH_SCHEMA,
        FILE_SUMMARY_SCHEMA,
        WORKSPACE_LIST_SCHEMA,
        WORKSPACE_READ_SCHEMA,
        WORKSPACE_SEARCH_SCHEMA,
        WORKSPACE_PATCH_SCHEMA,
        CREATE_PLAN_SCHEMA,
        UPDATE_STEP_SCHEMA,
        REVISE_PLAN_SCHEMA,
        COMMAND_SCHEMA,
    ]
}

ALL_TOOL_GUIDANCE = {
    "get_current_datetime": DATETIME_PROMPT,
    "search_web": SEARCH_PROMPT,
    "fetch_url": FETCH_PROMPT,
    "read_attached_file": FILE_READER_PROMPT,
    "search_attachment": FILE_READER_PROMPT,
    "summarize_attachment": FILE_READER_PROMPT,
    "list_workspace_files": WORKSPACE_PROMPT,
    "read_workspace_file": WORKSPACE_PROMPT,
    "search_workspace": WORKSPACE_PROMPT,
    "apply_workspace_patch": WORKSPACE_PROMPT,
    "create_plan": PLANNING_PROMPT,
    "update_step": PLANNING_PROMPT,
    "revise_plan": PLANNING_PROMPT,
    "run_workspace_command": COMMAND_PROMPT,
}


def get_tools(
    allowed_names: frozenset[str],
    runtime_handlers: dict | None = None,
) -> tuple[dict, list, str]:
    """Build the executable registry, schemas, and prompt for one mode."""
    unknown = allowed_names - ALL_TOOLS.keys()
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"mode references unknown tools: {names}")

    # Sorting makes prompts and schemas deterministic across runs even though
    # policies use sets for fast membership checks.
    names = sorted(allowed_names)
    handlers = ALL_TOOLS | (runtime_handlers or {})
    tools = {name: handlers[name] for name in names}
    schemas = [ALL_TOOL_SCHEMAS[name] for name in names]
    guidance = "\n".join(
        dict.fromkeys(ALL_TOOL_GUIDANCE[name] for name in names)
    )
    return tools, schemas, guidance
