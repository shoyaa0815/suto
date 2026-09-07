from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceSection:
    label: str
    text: str


@dataclass(frozen=True)
class DocumentChunk:
    index: int
    text: str
    sources: tuple[str, ...]
    token_count: int

    @property
    def citation(self) -> str:
        return ", ".join(self.sources)


@dataclass
class DocumentIndex:
    filename: str
    digest: str
    sections: list[SourceSection]
    chunks: list[DocumentChunk]
    vectors: list[Counter[str]]
    summaries: dict[str, str] = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        return sum(chunk.token_count for chunk in self.chunks)


Completion = Callable[[str, str, int], Awaitable[str]]
