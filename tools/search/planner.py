"""Query planner — decomposes complex questions into focused sub-queries.

Uses a model call to analyze the user question and produce 1–5 short
search queries that together cover every information need.
"""

import json

import aiohttp


PLANNER_PROMPT = """\
You are a search query planner. Given a user question, decompose it into
1–5 short web search queries (1–6 words each) that together cover every
distinct information need in the question.

Rules:
- Each query should target one specific fact, entity, or comparison point.
- Keep queries short — they go directly to a search engine.
- Do not combine unrelated topics into one query.
- For simple factual questions, return just one query.
- Return valid JSON only, no markdown fences.

Respond with exactly this JSON format:
{"sub_queries": ["query 1", "query 2"]}"""


async def plan_queries(
    session: aiohttp.ClientSession,
    question: str,
) -> list[str]:
    """Decompose *question* into sub-queries via a model call.

    Falls back to the original question as a single-element list if the
    model returns invalid JSON or an empty list.
    """
    try:
        from ai import client

        data = await client.chat(
            session,
            [
                {"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": question},
            ],
            [],
            think=False,
        )
        raw = data.get("message", {}).get("content", "").strip()
        # Strip markdown fences if the model wraps the JSON.
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        queries = parsed.get("sub_queries", [])
        if isinstance(queries, list) and queries:
            return [str(q) for q in queries if str(q).strip()][:5] or [question]
        return [question]
    except Exception:
        return [question]
