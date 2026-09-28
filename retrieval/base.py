"""Common result shape for information retrieved into model context."""

from dataclasses import dataclass
from typing import Protocol


class RetrievalError(RuntimeError):
    """A retrieval source could not provide results."""


@dataclass(frozen=True)
class RetrievalResult:
    source: str
    id: str
    content: str
    category: str = ""


class Retriever(Protocol):
    async def search(self, query: str, limit: int = 5) -> list[RetrievalResult]: ...
