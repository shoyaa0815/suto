"""Provider-independent retrieval contracts and local implementations."""

from .base import RetrievalError, RetrievalResult, Retriever
from .memory import MemoryRetriever

__all__ = ["RetrievalError", "RetrievalResult", "Retriever", "MemoryRetriever"]
