import math
import re
import time
import unicodedata
from collections import Counter

from .models import DocumentChunk, DocumentIndex
from .timing import log_timing


MAX_RETRIEVAL_CHUNKS = 6
DEFAULT_RETRIEVAL_CHUNKS = 4


def features(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    result: Counter[str] = Counter(f"word:{word}" for word in words)
    compact = "".join(char for char in normalized if char.isalnum())
    result.update(
        f"char:{compact[i:i + 3]}" for i in range(max(0, len(compact) - 2))
    )
    return result


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    common = left.keys() & right.keys()
    numerator = sum(left[key] * right[key] for key in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def retrieve_chunks(
    document: DocumentIndex,
    query: str,
    max_chunks: int = DEFAULT_RETRIEVAL_CHUNKS,
) -> list[DocumentChunk]:
    started = time.perf_counter()
    limit = min(max(int(max_chunks), 1), MAX_RETRIEVAL_CHUNKS)
    query_vector = features(query)
    normalized_query = unicodedata.normalize("NFKC", query).casefold().strip()
    scored = []
    for index, (chunk, vector) in enumerate(zip(document.chunks, document.vectors)):
        score = _cosine(query_vector, vector)
        if normalized_query and normalized_query in chunk.text.casefold():
            score += 1.0
        scored.append((score, index))
    scored.sort(reverse=True)

    if not scored or scored[0][0] <= 0:
        log_timing(
            "document_retrieval",
            started,
            chunks_total=len(document.chunks),
            chunks_selected=0,
            tokens=0,
        )
        return []

    selected: list[int] = []
    for score, index in scored:
        if score <= 0 and selected:
            break
        for candidate in (index, index - 1, index + 1):
            if 0 <= candidate < len(document.chunks) and candidate not in selected:
                selected.append(candidate)
                if len(selected) == limit:
                    break
        if len(selected) == limit:
            break
    selected.sort()
    result = [document.chunks[index] for index in selected]
    log_timing(
        "document_retrieval",
        started,
        chunks_total=len(document.chunks),
        chunks_selected=len(result),
        tokens=sum(chunk.token_count for chunk in result),
    )
    return result
