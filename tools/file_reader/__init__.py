"""Request-scoped attached-document tools.

The package re-exports the original ``tools.file_reader`` API so existing
callers do not need to change after the implementation was split by concern.
"""

from .extraction import SUPPORTED_EXTENSIONS, TEXT_EXTENSIONS, extract_text
from .indexing import MAX_CACHED_DOCUMENTS, clear_document_cache, get_document
from .models import Completion, DocumentChunk, DocumentIndex, SourceSection
from .retrieval import (
    DEFAULT_RETRIEVAL_CHUNKS,
    MAX_RETRIEVAL_CHUNKS,
    retrieve_chunks,
)
from .runtime import (
    DIRECT_READ_MAX_TOKENS,
    build_attachment_tools,
    build_read_attached_file,
)
from .schemas import PROMPT, READ_SCHEMA, SCHEMA, SEARCH_SCHEMA, SUMMARY_SCHEMA
from .summarization import REDUCE_INPUT_TOKENS, summarize_document
from .tokenization import (
    CHUNK_OVERLAP_TOKENS,
    CHUNK_TARGET_TOKENS,
    chunk_sections,
    estimate_tokens,
)

__all__ = [
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_TARGET_TOKENS",
    "Completion",
    "DEFAULT_RETRIEVAL_CHUNKS",
    "DIRECT_READ_MAX_TOKENS",
    "DocumentChunk",
    "DocumentIndex",
    "MAX_CACHED_DOCUMENTS",
    "MAX_RETRIEVAL_CHUNKS",
    "PROMPT",
    "READ_SCHEMA",
    "REDUCE_INPUT_TOKENS",
    "SCHEMA",
    "SEARCH_SCHEMA",
    "SUMMARY_SCHEMA",
    "SUPPORTED_EXTENSIONS",
    "SourceSection",
    "TEXT_EXTENSIONS",
    "build_attachment_tools",
    "build_read_attached_file",
    "chunk_sections",
    "clear_document_cache",
    "estimate_tokens",
    "extract_text",
    "get_document",
    "retrieve_chunks",
    "summarize_document",
]
