"""Web search, URL fetching, and research pipeline.

This package re-exports the search and fetch tools so existing callers and tests
can import from ``tools.search`` transparently.
"""

import asyncio

from .fetch import fetch_url
from .fetch import PROMPT as FETCH_PROMPT
from .fetch import SCHEMA as FETCH_SCHEMA
from .pipeline import research
from .pipeline import PROMPT as RESEARCH_PROMPT
from .pipeline import SCHEMA as RESEARCH_SCHEMA
from .web import (
    DEFAULT_MAX_RESULTS,
    HARD_MAX_RESULTS,
    PROMPT as SEARCH_PROMPT,
    SCHEMA as SEARCH_SCHEMA,
    _cache,
    _last_call_time,
    _query_searxng,
    search_web,
    search_web_raw,
)

__all__ = [
    "DEFAULT_MAX_RESULTS",
    "FETCH_PROMPT",
    "FETCH_SCHEMA",
    "HARD_MAX_RESULTS",
    "RESEARCH_PROMPT",
    "RESEARCH_SCHEMA",
    "SEARCH_PROMPT",
    "SEARCH_SCHEMA",
    "_cache",
    "_last_call_time",
    "_query_searxng",
    "asyncio",
    "fetch_url",
    "research",
    "search_web",
    "search_web_raw",
]
