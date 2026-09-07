import time

from .models import Completion, DocumentIndex
from .timing import log_timing
from .tokenization import estimate_tokens


REDUCE_INPUT_TOKENS = 7000


def _group_by_token_budget(items: list[str], budget: int) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    tokens = 0
    for item in items:
        item_tokens = estimate_tokens(item)
        if current and tokens + item_tokens > budget:
            groups.append(current)
            current = []
            tokens = 0
        current.append(item)
        tokens += item_tokens
    if current:
        groups.append(current)
    return groups


async def summarize_document(
    document: DocumentIndex,
    complete: Completion,
    detail: str = "standard",
) -> str:
    detail = detail if detail in {"brief", "standard", "detailed"} else "standard"
    cached = document.summaries.get(detail)
    if cached is not None:
        print(
            f"[timing] event=document_summary_cache ms=0 hit=true "
            f"chunks={len(document.chunks)} detail={detail}"
        )
        return cached
    if not document.chunks:
        return f"[no extractable text in {document.filename}]"

    started = time.perf_counter()
    map_output_tokens = {"brief": 140, "standard": 260, "detailed": 420}[detail]
    final_output_tokens = {"brief": 300, "standard": 650, "detailed": 1100}[
        detail
    ]
    notes = []
    map_system = (
        "Create compact factual notes from one document chunk. Preserve all "
        "important names, dates, numbers, conditions, and warnings. Do not "
        "translate; keep the source language. Do not add facts. Include the "
        "provided source label with every point. Output notes only."
    )
    for chunk in document.chunks:
        note = await complete(
            map_system,
            f"Source: {chunk.citation}\n\n{chunk.text}",
            map_output_tokens,
        )
        notes.append(f"[source: {chunk.citation}]\n{note}")

    level = 0
    reduce_system = (
        "Merge document notes into a faithful, non-redundant summary. Preserve "
        "names, dates, numbers, conditions, warnings, and source labels. Do not "
        "translate and do not add facts. Output the merged summary only."
    )
    while len(notes) > 1:
        groups = _group_by_token_budget(notes, REDUCE_INPUT_TOKENS)
        output_limit = (
            final_output_tokens if len(groups) == 1 else map_output_tokens * 2
        )
        notes = [
            await complete(
                reduce_system,
                "\n\n---\n\n".join(group),
                output_limit,
            )
            for group in groups
        ]
        level += 1

    all_sources = tuple(
        dict.fromkeys(source for chunk in document.chunks for source in chunk.sources)
    )
    summary = f"{notes[0]}\n\nSources covered: {', '.join(all_sources)}"
    document.summaries[detail] = summary
    log_timing(
        "document_summary",
        started,
        cache_hit="false",
        chunks=len(document.chunks),
        reduce_levels=level,
        detail=detail,
    )
    return summary
