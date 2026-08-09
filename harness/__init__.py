# This package is the single place that "controls" every tool the AI can
# call. Each tool lives in its own module (datetime_tool.py, search.py,
# fetch.py, ...) and exports three things:
#   - a handler function (e.g. search_web)       -> what the tool does
#   - SCHEMA                                      -> how Ollama calls it
#   - PROMPT                                       -> when the AI should call it
#
# To add a new tool: create harness/your_tool.py with those three things,
# then add it to the imports and the three dicts/lists below. Nothing
# outside this package needs to change — ai.py only ever imports TOOLS,
# TOOL_SCHEMAS and TOOL_GUIDANCE from here.
from .datetime_tool import get_current_datetime
from .datetime_tool import PROMPT as DATETIME_PROMPT
from .datetime_tool import SCHEMA as DATETIME_SCHEMA
from .fetch import fetch_url
from .fetch import PROMPT as FETCH_PROMPT
from .fetch import SCHEMA as FETCH_SCHEMA
from .search import search_web
from .search import PROMPT as SEARCH_PROMPT
from .search import SCHEMA as SEARCH_SCHEMA

TOOLS = {
    "get_current_datetime": get_current_datetime,
    "search_web": search_web,
    "fetch_url": fetch_url,
}

TOOL_SCHEMAS = [DATETIME_SCHEMA, SEARCH_SCHEMA, FETCH_SCHEMA]

# Combined per-tool usage rules, ready to drop into ai.py's system prompt.
# This is deliberately just the tool-specific "when to call it" rules —
# identity, tone, and any rule that isn't about a specific tool stay in
# ai.py's own SYSTEM_PROMPT, not here.
TOOL_GUIDANCE = "\n".join([DATETIME_PROMPT, SEARCH_PROMPT, FETCH_PROMPT])
