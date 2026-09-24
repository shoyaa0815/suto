"""Web search and URL fetch operations adapter."""

from tools.search.fetch import fetch_url
from tools.search.web import search_web


class WebOperations:
    """Safe web operations providing web search and bounded URL fetch."""

    @staticmethod
    async def search(query: str) -> str:
        """Search the web for a query."""
        return await search_web(query)

    @staticmethod
    async def fetch(url: str) -> str:
        """Fetch content from an allowed HTTP/HTTPS URL."""
        return await fetch_url(url)
