"""Source ledger for tracking retrieved web sources.

Assigns sequential IDs to each source at retrieval time so the synthesizer
can reference them as ``[1]``, ``[2]``, etc.  The ledger is ephemeral —
it lives only for the duration of a single ``research()`` call.
"""

from dataclasses import dataclass, field


@dataclass
class Source:
    id: int
    url: str
    title: str
    snippet: str
    full_text: str | None = None


class SourceLedger:
    """Tracks sources discovered during a research pipeline run."""

    def __init__(self) -> None:
        self._sources: list[Source] = []
        self._urls: set[str] = set()

    def register(self, url: str, title: str, snippet: str) -> Source:
        """Register a new source.  Duplicate URLs are silently skipped."""
        if url in self._urls:
            return next(s for s in self._sources if s.url == url)
        source = Source(
            id=len(self._sources) + 1,
            url=url,
            title=title,
            snippet=snippet,
        )
        self._sources.append(source)
        self._urls.add(url)
        return source

    def enrich(self, source_id: int, full_text: str) -> None:
        """Attach full page text to a previously registered source."""
        for source in self._sources:
            if source.id == source_id:
                source.full_text = full_text
                return

    def get(self, source_id: int) -> Source | None:
        for source in self._sources:
            if source.id == source_id:
                return source
        return None

    @property
    def sources(self) -> list[Source]:
        return list(self._sources)

    def __len__(self) -> int:
        return len(self._sources)

    def to_context(self) -> str:
        """Format all sources for inclusion in a model prompt."""
        if not self._sources:
            return "No sources available."
        parts = []
        for s in self._sources:
            part = f"[{s.id}] {s.title} — {s.url}\nSnippet: {s.snippet}"
            if s.full_text:
                part += f"\nFull content: {s.full_text}"
            parts.append(part)
        return "\n\n".join(parts)

    def to_footer(self, cited_ids: set[int] | None = None) -> str:
        """Build a 'Sources:' footer listing URLs.

        If *cited_ids* is given only those sources are listed; otherwise
        every source in the ledger is included.
        """
        sources = (
            [s for s in self._sources if s.id in cited_ids]
            if cited_ids
            else self._sources
        )
        if not sources:
            return ""
        lines = ["", "Sources:"]
        for s in sources:
            lines.append(f"[{s.id}] {s.title} — {s.url}")
        return "\n".join(lines)
