"""Research pipeline — orchestrates multi-step search, extraction, and synthesis.

Exposed as the ``research`` tool which the AI can call for complex questions
that need multiple sources.  Internally it:

1. Decomposes the question into sub-queries (query planner).
2. Runs concurrent web searches and deduplicates results.
3. Selects the most promising URLs via a lightweight model call.
4. Fetches full page content for selected URLs.
5. Synthesizes a cited answer from the source ledger.
"""

import asyncio
import json
from urllib.parse import urlparse

import aiohttp

from .fetch import fetch_url
from .ledger import SourceLedger
from .planner import plan_queries
from .synthesizer import synthesize
from .web import search_web_raw

# How many URLs to deep-fetch after the search phase.
EXTRACT_TOP_N = 3

PROMPT = """- Use research for complex questions needing multiple sources, comparisons,
  or comprehensive answers. It automatically decomposes the question, searches
  multiple angles, reads full pages, and synthesizes a cited answer.
- Use search_web for simple factual lookups (one topic, one answer).
- Do not call research and search_web for the same question.
- research is expensive (multiple search + model calls). Use it deliberately."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "research",
        "description": (
            "Research a question thoroughly using multi-step web search, "
            "extraction, and synthesis. Use for complex questions that need "
            "multiple sources. For simple lookups, prefer search_web."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to research comprehensively.",
                },
            },
            "required": ["question"],
        },
    },
}


_SELECTOR_PROMPT = """\
You are a search result selector. Given a user question and a list of search
results, pick the {n} most relevant results to read in full.

Return a JSON array of result indices (0-based) that are most likely to
contain the answer. Return valid JSON only, no markdown fences.

Example: [0, 2, 4]"""


def _dedup_results(all_results: list[dict]) -> list[dict]:
    """Deduplicate search results by URL across sub-query batches."""
    seen = set()
    deduped = []
    for r in all_results:
        url = r.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        deduped.append(r)
    return deduped


async def _select_urls(
    session: aiohttp.ClientSession,
    question: str,
    results: list[dict],
    top_n: int,
) -> list[int]:
    """Use a model call to pick the most relevant result indices.

    Falls back to the first *top_n* indices if the model returns invalid
    output.
    """
    if len(results) <= top_n:
        return list(range(len(results)))

    listing = "\n".join(
        f"[{i}] {r['title']} — {r['url']}\n    {r['snippet']}"
        for i, r in enumerate(results)
    )
    try:
        from ai import client

        data = await client.chat(
            session,
            [
                {
                    "role": "system",
                    "content": _SELECTOR_PROMPT.format(n=top_n),
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\nResults:\n{listing}"
                    ),
                },
            ],
            [],
            think=False,
        )
        raw = data.get("message", {}).get("content", "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        indices = json.loads(raw)
        if isinstance(indices, list):
            valid = [
                int(i)
                for i in indices
                if isinstance(i, (int, float)) and 0 <= int(i) < len(results)
            ]
            if valid:
                return valid[:top_n]
    except Exception:
        pass
    return list(range(min(top_n, len(results))))


async def research(question: str) -> str:
    """Run the full research pipeline for *question*.

    This is the tool handler registered as ``research`` in the tool registry.
    It creates its own ``aiohttp.ClientSession`` for model calls, matching
    the pattern used by ``search_web`` and ``fetch_url``.
    """
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1. Plan queries
        sub_queries = await plan_queries(session, question)

        # 2. Search phase — run sub-queries concurrently.
        search_tasks = [search_web_raw(q) for q in sub_queries]
        search_results = await asyncio.gather(*search_tasks)

        # Flatten and deduplicate.
        all_results = _dedup_results(
            [r for batch in search_results for r in batch]
        )

        if not all_results:
            return "ไม่สามารถค้นหาข้อมูลได้ในขณะนี้"

        # 3. Register all results in the source ledger.
        ledger = SourceLedger()
        for r in all_results:
            ledger.register(r["url"], r["title"], r["snippet"])

        # 4. Select & extract top URLs.
        selected_indices = await _select_urls(
            session, question, all_results, EXTRACT_TOP_N
        )
        fetch_tasks = []
        selected_sources = []
        for idx in selected_indices:
            r = all_results[idx]
            source = ledger.get(
                next(s.id for s in ledger.sources if s.url == r["url"])
            )
            if source:
                selected_sources.append(source)
                fetch_tasks.append(fetch_url(r["url"]))

        if fetch_tasks:
            fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            for source, text in zip(selected_sources, fetched):
                if isinstance(text, str) and text not in (
                    "couldn't fetch that url",
                    "page has no readable text content",
                ):
                    ledger.enrich(source.id, text)

        # 5. Synthesize cited answer.
        return await synthesize(session, question, ledger)
