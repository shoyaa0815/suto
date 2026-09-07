import hashlib
import time
from collections import OrderedDict

from .extraction import read_sections
from .models import DocumentIndex
from .retrieval import features
from .timing import log_timing
from .tokenization import chunk_sections


MAX_CACHED_DOCUMENTS = 8
_DOCUMENT_CACHE: OrderedDict[str, DocumentIndex] = OrderedDict()


def _document_digest(filename: str, data: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return hashlib.sha256(ext.encode() + b"\0" + data).hexdigest()


def get_document(filename: str, data: bytes) -> DocumentIndex:
    digest = _document_digest(filename, data)
    cached = _DOCUMENT_CACHE.get(digest)
    if cached is not None:
        _DOCUMENT_CACHE.move_to_end(digest)
        print(
            f"[timing] event=document_cache ms=0 hit=true "
            f"chunks={len(cached.chunks)} tokens={cached.token_count}"
        )
        return cached

    started = time.perf_counter()
    extraction_started = time.perf_counter()
    sections = [section for section in read_sections(filename, data) if section.text]
    log_timing(
        "document_extraction",
        extraction_started,
        bytes=len(data),
        sections=len(sections),
    )
    chunk_started = time.perf_counter()
    chunks = chunk_sections(sections)
    log_timing(
        "document_chunking",
        chunk_started,
        chunks=len(chunks),
        tokens=sum(chunk.token_count for chunk in chunks),
    )
    vector_started = time.perf_counter()
    vectors = [features(chunk.text) for chunk in chunks]
    log_timing("document_vectors", vector_started, chunks=len(chunks))
    index = DocumentIndex(
        filename=filename,
        digest=digest,
        sections=sections,
        chunks=chunks,
        vectors=vectors,
    )
    _DOCUMENT_CACHE[digest] = index
    _DOCUMENT_CACHE.move_to_end(digest)
    while len(_DOCUMENT_CACHE) > MAX_CACHED_DOCUMENTS:
        _DOCUMENT_CACHE.popitem(last=False)
    log_timing(
        "document_index",
        started,
        cache_hit="false",
        bytes=len(data),
        sections=len(sections),
        chunks=len(chunks),
        tokens=index.token_count,
    )
    return index


def clear_document_cache() -> None:
    _DOCUMENT_CACHE.clear()
