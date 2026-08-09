import asyncio
import time

import aiohttp

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

_cache: dict[str, tuple[float, str]] = {}
_last_call_time = 0.0

# PROMPT is this tool's slice of the AI's system prompt: when to call
# search_web, when not to, and how to avoid hammering it. See the comment
# in datetime_tool.py for why this lives next to the tool instead of in
# ai.py — same reasoning applies here.
PROMPT = """- Use search_web only for things you can't already answer correctly:
  current events, recent releases, prices, or anything time-sensitive.
- Do not search for general knowledge, definitions, or things you already know.
- Your knowledge of whether a specific product has launched, is available in
  a given region, or what it costs may be outdated — training data has a
  cutoff and time has passed since then. Never answer these from memory,
  even if you're confident. Always confirm with search_web first.
- search_web is expensive and rate-limited: never repeat the exact same or a
  rephrased query — reuse what that search already returned instead. But
  tasks that genuinely need several different lookups (e.g. checking the
  price at each of several different stores) may call it once per distinct
  query needed.
- If search_web comes back saying it's unavailable, tell the user that and
  answer with what you already know. Do not retry it."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "Search the web for current information.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
            },
            "required": ["query"],
        },
    },
}


async def search_web(query: str) -> str:
    global _last_call_time

    key = query.strip().lower()
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
            async with session.get(
                SEARXNG_URL, params={"q": query, "format": "json"}, headers=HEADERS
            ) as response:
                response.raise_for_status()
                data = await response.json()
    except Exception as e:
        print(f"[search_web] {type(e).__name__}: {e!r}")
        return "web search is unavailable right now"

    results = data.get("results", [])[:5]
    result = (
        "\n\n".join(
            f"{r.get('title', '')} - {r.get('url', '')}\n{r.get('content', '')}"
            for r in results
        )
        if results
        else "no results found"
    )
    _cache[key] = (now, result)
    return result
