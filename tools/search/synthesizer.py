"""Answer synthesizer — produces a cited answer from source ledger content.

Uses a model call to combine evidence from multiple sources into a single
coherent answer with inline ``[N]`` citations and a sources footer.
"""

import re

import aiohttp

from .ledger import SourceLedger


SYNTHESIZER_PROMPT = """\
You are a research synthesizer. Given a question and numbered source
materials, produce a comprehensive answer grounded strictly in those sources.

Rules:
- Answer ONLY from the provided source content. Never add facts from memory.
- Cite sources inline as [1], [2], etc. matching the source IDs given.
- Use at most 3 citations per sentence.
- When sources disagree, present both perspectives with their citation IDs.
- If no source answers a specific aspect of the question, say so explicitly
  rather than guessing.
- Never fabricate citation IDs or URLs.
- Answer in the same language as the user's question.
- Be concise and direct."""


async def synthesize(
    session: aiohttp.ClientSession,
    question: str,
    ledger: SourceLedger,
) -> str:
    """Synthesize a cited answer from the source ledger.

    Returns the answer text with inline ``[N]`` citations and a sources
    footer listing cited URLs.
    """
    if not ledger.sources:
        return "ไม่สามารถค้นหาข้อมูลได้ในขณะนี้"

    from ai import client

    context = ledger.to_context()
    data = await client.chat(
        session,
        [
            {"role": "system", "content": SYNTHESIZER_PROMPT},
            {
                "role": "user",
                "content": f"Question: {question}\n\nSources:\n{context}",
            },
        ],
        [],
        think=False,
    )
    answer = data.get("message", {}).get("content", "").strip()
    if not answer:
        return "ไม่สามารถสรุปข้อมูลได้ในขณะนี้"

    # Extract cited IDs from the answer to build a minimal footer.
    cited_ids = {int(m) for m in re.findall(r"\[(\d+)\]", answer)}
    footer = ledger.to_footer(cited_ids)
    return answer + footer if footer else answer
