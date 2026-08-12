import asyncio
import time
from urllib.parse import urlparse

import aiohttp

from .fetch import fetch_url

SEARXNG_URL = "http://localhost:8080/search"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# how long a search result is reused for the same query, and the minimum
# gap enforced between actual requests sent to SearXNG. Both exist to avoid
# hammering upstream search engines (e.g. Wikidata) into rate-limiting us.
CACHE_TTL_SECONDS = 300
MIN_INTERVAL_SECONDS = 3

# Search result snippets are often too short to answer anything specific
# (price, spec, exact date). To close that gap without fetching every
# result, the top FETCH_TOP_N results get their full page text pulled in
# via fetch_url and appended under the snippet, capped at ENRICH_MAX_CHARS
# each so a couple of long pages can't blow out the context window.
FETCH_TOP_N = 2
ENRICH_MAX_CHARS = 1500

# Bounds for the AI-controlled max_results parameter (see SCHEMA below).
DEFAULT_MAX_RESULTS = 5
HARD_MAX_RESULTS = 10

# A request to SearXNG is retried once, after a short pause, before search_web
# gives up and reports itself unavailable — one bad round-trip shouldn't cost
# a whole search when a second attempt usually goes through.
RETRY_DELAY_SECONDS = 1

_cache: dict[str, tuple[float, str]] = {}
_last_call_time = 0.0

# PROMPT is this tool's slice of the AI's system prompt: when to call
# search_web, when not to, and how to avoid hammering it. See the comment
# in datetime_tool.py for why this lives next to the tool instead of in
# ai.py — same reasoning applies here.
PROMPT = """- Use search_web only for things you can't already answer correctly:
  current events, recent releases, prices, or anything time-sensitive, or a
  named person/place/thing you don't already know for certain. If a query
  mentions "latest", "current", "still", or a role/status that changes over
  time (e.g. who currently holds a position), that is a signal to search
  even if you feel confident.
- Do not search for general knowledge, definitions, math, or settled history.
- Your knowledge of whether a specific product has launched, is available in
  a given region, or what it costs may be outdated — training data has a
  cutoff and time has passed since then. Never answer these from memory,
  even if you're confident. Always confirm with search_web first.
- Keep each query short (1-6 words), not a full sentence. If the question
  covers more than one topic or name, search each one separately instead of
  combining them into one query — a combined query returns shallow results
  for every part.
- search_web is expensive and rate-limited: never repeat the exact same or a
  rephrased query — reuse what that search already returned instead. But
  tasks that genuinely need several different lookups (e.g. checking the
  price at each of several different stores, or several unrelated topics in
  one question) may call it once per distinct query needed.
- Before answering, check whether every part of the question is actually
  backed by a search result you got back. If a specific number, name, or
  claim isn't in what search_web or fetch_url returned, don't state it —
  say you couldn't confirm it instead of filling the gap from memory.
- If two results disagree, mention both instead of silently picking one.
- If search_web comes back saying it's unavailable, tell the user that and
  answer with what you already know. Do not retry it.
- Refuse to search for anything intended to find extremist material, abuse
  content, or malware — decline the request instead of running the search."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "Search the web for current information.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {
                    "type": "integer",
                    "description": f"Number of results to return (default "
                    f"{DEFAULT_MAX_RESULTS}, max {HARD_MAX_RESULTS}).",
                },
            },
            "required": ["query"],
        },
    },
}


def _dedup_by_domain(results: list[dict]) -> list[dict]:
    seen_domains = set()
    deduped = []
    for r in results:
        domain = urlparse(r.get("url", "")).netloc
        if domain and domain in seen_domains:
            continue
        seen_domains.add(domain)
        deduped.append(r)
    return deduped


async def _query_searxng(session: aiohttp.ClientSession, query: str) -> dict:
    async with session.get(
        SEARXNG_URL, params={"q": query, "format": "json"}, headers=HEADERS
    ) as response:
        response.raise_for_status()
        return await response.json()


async def search_web(query: str, max_results: int | None = None) -> str:
    global _last_call_time

    requested = DEFAULT_MAX_RESULTS if max_results is None else max_results
    limit = min(max(int(requested), 1), HARD_MAX_RESULTS)
    key = f"{query.strip().lower()}|{limit}"
    now = time.monotonic()

    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    wait = MIN_INTERVAL_SECONDS - (now - _last_call_time)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call_time = time.monotonic()

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                data = await _query_searxng(session, query)
            except Exception as e:
                print(f"[search_web] retrying after {type(e).__name__}: {e!r}")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                data = await _query_searxng(session, query)
    except Exception as e:
        print(f"[search_web] {type(e).__name__}: {e!r}")
        return "web search is unavailable right now"

    results = _dedup_by_domain(data.get("results", []))[:limit]
    if not results:
        result = "no results found"
    else:
        entries = []
        for i, r in enumerate(results):
            entry = f"{r.get('title', '')} - {r.get('url', '')}\n{r.get('content', '')}"
            url = r.get("url")
            if i < FETCH_TOP_N and url:
                page_text = await fetch_url(url)
                if page_text and page_text not in (
                    "couldn't fetch that url",
                    "page has no readable text content",
                ):
                    entry += f"\nFull page content: {page_text[:ENRICH_MAX_CHARS]}"
            entries.append(entry)
        result = "\n\n".join(entries)

    _cache[key] = (now, result)
    return result
