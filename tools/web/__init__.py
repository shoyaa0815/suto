"""Web tool module exposing schemas and operations."""

from typing import Any

from .operations import WebOperations

_default_web = WebOperations()


async def search(query: str) -> str:
    """Search the web."""
    return await _default_web.search(query)


async def fetch(url: str) -> str:
    """Fetch URL contents."""
    return await _default_web.fetch(url)


web_search = search
web_fetch = fetch

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web.search",
        "description": "Search the web for up-to-date information on a query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string.",
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for up-to-date information on a query.",
        "parameters": SEARCH_SCHEMA["function"]["parameters"],
    },
}

FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web.fetch",
        "description": "Fetch text content from a public HTTP or HTTPS web page.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch content from.",
                },
            },
            "required": ["url"],
        },
    },
}

FETCH_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "Fetch text content from a public HTTP or HTTPS web page.",
        "parameters": FETCH_SCHEMA["function"]["parameters"],
    },
}

PROMPT = (
    "Use web.search (or web_search) when the user asks about current events, "
    "news, or information beyond your cutoff date. Use web.fetch (or web_fetch) "
    "when you have a specific URL to inspect."
)

WEB_SCHEMAS = [
    SEARCH_SCHEMA,
    SEARCH_ALIAS_SCHEMA,
    FETCH_SCHEMA,
    FETCH_ALIAS_SCHEMA,
]

WEB_TOOLS: dict[str, Any] = {
    "web.search": search,
    "web_search": search,
    "web.fetch": fetch,
    "web_fetch": fetch,
}

__all__ = [
    "FETCH_ALIAS_SCHEMA",
    "FETCH_SCHEMA",
    "PROMPT",
    "SEARCH_ALIAS_SCHEMA",
    "SEARCH_SCHEMA",
    "WEB_SCHEMAS",
    "WEB_TOOLS",
    "WebOperations",
    "fetch",
    "search",
    "web_fetch",
    "web_search",
]
