"""Provider-independent retrieval contracts; legacy adapters load on demand."""

from .base import RetrievalError, RetrievalResult, Retriever

__all__ = ["RetrievalError", "RetrievalResult", "Retriever", "MemoryRetriever"]


def __getattr__(name: str):
    # Preserve explicit legacy imports without loading personal memory in Jobs.
    if name == "MemoryRetriever":
        from .memory import MemoryRetriever

        return MemoryRetriever
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
