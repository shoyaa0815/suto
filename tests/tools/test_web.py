import pytest
from unittest.mock import AsyncMock, patch

from tools.web import WebOperations, fetch, search, web_fetch, web_search


@pytest.mark.asyncio
async def test_web_search_delegation():
    with patch("tools.web.operations.search_web", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = "search results mock"
        res = await search("python news")
        assert res == "search results mock"
        mock_search.assert_called_once_with("python news")

        res2 = await web_search("python 3.12")
        assert res2 == "search results mock"


@pytest.mark.asyncio
async def test_web_fetch_delegation():
    with patch("tools.web.operations.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = "page content mock"
        res = await fetch("https://example.com")
        assert res == "page content mock"
        mock_fetch.assert_called_once_with("https://example.com")

        res2 = await web_fetch("https://example.org")
        assert res2 == "page content mock"


@pytest.mark.asyncio
async def test_web_operations_class():
    with patch("tools.web.operations.search_web", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = "ok"
        assert await WebOperations.search("query") == "ok"
    with patch("tools.web.operations.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = "ok"
        assert await WebOperations.fetch("https://url") == "ok"
