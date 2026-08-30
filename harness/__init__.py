# This package is the single place that "controls" every tool the AI can
# call. Each tool lives in its own module (datetime_tool.py, search.py,
# fetch.py, ...) and exports three things:
#   - a handler function (e.g. search_web)       -> what the tool does
#   - SCHEMA                                      -> how Ollama calls it
#   - PROMPT                                       -> when the AI should call it
#
# To add a new tool: create harness/your_tool.py with those three things,
# then add it to the imports and the three dicts/lists below. Nothing
# outside this package needs to change. ai.py asks this registry for the tools
# allowed by the active mode instead of exposing every tool globally.
from .datetime_tool import get_current_datetime
from .datetime_tool import PROMPT as DATETIME_PROMPT
from .datetime_tool import SCHEMA as DATETIME_SCHEMA
from .fetch import fetch_url
from .fetch import PROMPT as FETCH_PROMPT
from .fetch import SCHEMA as FETCH_SCHEMA
from .search import search_web
from .search import PROMPT as SEARCH_PROMPT
from .search import SCHEMA as SEARCH_SCHEMA

ALL_TOOLS = {
    "get_current_datetime": get_current_datetime,
    "search_web": search_web,
    "fetch_url": fetch_url,
}

ALL_TOOL_SCHEMAS = {
    schema["function"]["name"]: schema
    for schema in [DATETIME_SCHEMA, SEARCH_SCHEMA, FETCH_SCHEMA]
}

ALL_TOOL_GUIDANCE = {
    "get_current_datetime": DATETIME_PROMPT,
    "search_web": SEARCH_PROMPT,
    "fetch_url": FETCH_PROMPT,
}


def get_tools(allowed_names: frozenset[str]) -> tuple[dict, list, str]:
    """Build the executable registry, schemas, and prompt for one mode."""
    unknown = allowed_names - ALL_TOOLS.keys()
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"mode references unknown tools: {names}")

    # Sorting makes prompts and schemas deterministic across runs even though
    # policies use sets for fast membership checks.
    names = sorted(allowed_names)
    tools = {name: ALL_TOOLS[name] for name in names}
    schemas = [ALL_TOOL_SCHEMAS[name] for name in names]
    guidance = "\n".join(ALL_TOOL_GUIDANCE[name] for name in names)
    return tools, schemas, guidance
