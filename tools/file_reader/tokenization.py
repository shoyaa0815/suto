import math
import re
import unicodedata

from .models import DocumentChunk, SourceSection


# Conservative model-token estimates, not character limits. Thai and CJK
# characters are counted individually; Latin runs use roughly four characters
# per token. This avoids bundling another copy of Qwen's tokenizer into the bot.
CHUNK_TARGET_TOKENS = 2500
CHUNK_OVERLAP_TOKENS = 180


def estimate_tokens(text: str) -> int:
    """Conservatively estimate Qwen input tokens for chunk budgeting."""
    total = 0
    latin_run = 0

    def flush_latin() -> None:
        nonlocal total, latin_run
        if latin_run:
            total += max(1, math.ceil(latin_run / 4))
            latin_run = 0

    for char in text:
        if char.isspace():
            flush_latin()
            continue
        name = unicodedata.name(char, "")
        if "LATIN" in name or char.isdigit() or char == "_":
            latin_run += 1
        else:
            flush_latin()
            total += 1
    flush_latin()
    return total


def _split_oversized_text(text: str, token_limit: int) -> list[str]:
    if estimate_tokens(text) <= token_limit:
        return [text]
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s+|\n+", text)
        if part.strip()
    ]
    if len(sentences) > 1:
        pieces = []
        current = []
        current_tokens = 0
        for sentence in sentences:
            sentence_tokens = estimate_tokens(sentence)
            if current and current_tokens + sentence_tokens > token_limit:
                pieces.append(" ".join(current))
                current = []
                current_tokens = 0
            if sentence_tokens > token_limit:
                pieces.extend(_split_oversized_text(sentence, token_limit))
            else:
                current.append(sentence)
                current_tokens += sentence_tokens
        if current:
            pieces.append(" ".join(current))
        return pieces

    pieces = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        best = low
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(text[start:middle]) <= token_limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        pieces.append(text[start:best].strip())
        start = best
    return [piece for piece in pieces if piece]


def chunk_sections(
    sections: list[SourceSection],
    target_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[DocumentChunk]:
    atoms: list[tuple[str, str, int]] = []
    for section in sections:
        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(r"\n\s*\n", section.text)
            if paragraph.strip()
        ]
        for paragraph in paragraphs:
            for piece in _split_oversized_text(paragraph, target_tokens):
                atoms.append((piece, section.label, estimate_tokens(piece)))

    chunks: list[DocumentChunk] = []
    current: list[tuple[str, str, int]] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        sources = tuple(dict.fromkeys(source for _, source, _ in current))
        chunks.append(
            DocumentChunk(
                index=len(chunks),
                text="\n\n".join(text for text, _, _ in current),
                sources=sources,
                token_count=current_tokens,
            )
        )
        overlap: list[tuple[str, str, int]] = []
        overlap_size = 0
        for atom in reversed(current):
            if atom[2] > overlap_tokens:
                break
            if overlap and overlap_size + atom[2] > overlap_tokens:
                break
            overlap.insert(0, atom)
            overlap_size += atom[2]
            if overlap_size >= overlap_tokens:
                break
        current = overlap
        current_tokens = overlap_size

    for atom in atoms:
        if current and current_tokens + atom[2] > target_tokens:
            flush()
        if current and current_tokens + atom[2] > target_tokens:
            current = []
            current_tokens = 0
        current.append(atom)
        current_tokens += atom[2]
    flush()
    return chunks
