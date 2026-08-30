import pytest

from tools import search


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    # search_web's cache and rate-limit clock are module-level globals, so
    # every test needs a clean slate or an earlier test's cached result /
    # last-call time would leak in and make this test's behavior depend on
    # run order.
    search._cache.clear()
    monkeypatch.setattr(search, "_last_call_time", 0.0)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Real asyncio.sleep would make the rate-limit test take ~3 real
    # seconds. Replace it with a no-op that just records what it was asked
    # to wait for, so tests can assert on that instead of on wall time.
    calls = []

    async def _fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(search.asyncio, "sleep", _fake_sleep)
    return calls


def _raw_results(n: int) -> list[dict]:
    return [
        {
            "title": f"Title {i}",
            "url": f"https://example{i}.com/page",
            "content": f"snippet {i}",
        }
        for i in range(n)
    ]


def _stub_query_searxng(monkeypatch, *items):
    """Replace search._query_searxng with a stub that returns/raises each
    item in `items` in order, one per call. Items that are Exception
    instances are raised instead of returned."""
    calls = list(items)

    async def _fake(session, query):
        if not calls:
            raise AssertionError("_query_searxng called more times than stubbed")
        item = calls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(search, "_query_searxng", _fake)


def _stub_fetch_url(monkeypatch):
    """Replace search.fetch_url with a stub that returns a fixed string per
    url and records every url it was called with."""
    called_urls = []

    async def _fake(url):
        called_urls.append(url)
        return f"FULL CONTENT for {url}"

    monkeypatch.setattr(search, "fetch_url", _fake)
    return called_urls


def _entries(result: str) -> list[str]:
    return result.split("\n\n")


class TestCache:
    async def test_same_query_within_ttl_returns_cached_result_without_new_call(
        self, monkeypatch
    ):
        _stub_query_searxng(monkeypatch, {"results": _raw_results(1)})
        _stub_fetch_url(monkeypatch)

        first = await search.search_web("python")
        second = await search.search_web("python")

        assert first == second

    async def test_different_max_results_is_a_different_cache_key(self, monkeypatch):
        _stub_query_searxng(
            monkeypatch,
            {"results": _raw_results(5)},
            {"results": _raw_results(5)},
        )
        _stub_fetch_url(monkeypatch)

        five = await search.search_web("python", max_results=5)
        three = await search.search_web("python", max_results=3)

        assert len(_entries(five)) == 5
        assert len(_entries(three)) == 3


class TestRateLimit:
    async def test_back_to_back_different_queries_are_spaced_out(
        self, monkeypatch, _no_real_sleep
    ):
        _stub_query_searxng(
            monkeypatch,
            {"results": _raw_results(1)},
            {"results": _raw_results(1)},
        )
        _stub_fetch_url(monkeypatch)

        await search.search_web("python")
        await search.search_web("javascript")

        assert any(wait > 0 for wait in _no_real_sleep)


class TestDedup:
    async def test_same_domain_results_are_collapsed_to_one(self, monkeypatch):
        results = [
            {"title": "A", "url": "https://example.com/a", "content": "a"},
            {"title": "B", "url": "https://example.com/b", "content": "b"},
            {"title": "C", "url": "https://other.com/c", "content": "c"},
        ]
        _stub_query_searxng(monkeypatch, {"results": results})
        _stub_fetch_url(monkeypatch)

        result = await search.search_web("query")

        assert len(_entries(result)) == 2
        assert "example.com/a" in result
        assert "example.com/b" not in result
        assert "other.com/c" in result


class TestMaxResults:
    async def test_defaults_to_default_max_results(self, monkeypatch):
        _stub_query_searxng(monkeypatch, {"results": _raw_results(7)})
        _stub_fetch_url(monkeypatch)

        result = await search.search_web("query")

        assert len(_entries(result)) == search.DEFAULT_MAX_RESULTS

    async def test_respects_requested_max_results(self, monkeypatch):
        _stub_query_searxng(monkeypatch, {"results": _raw_results(7)})
        _stub_fetch_url(monkeypatch)

        result = await search.search_web("query", max_results=3)

        assert len(_entries(result)) == 3

    async def test_clamped_to_hard_max(self, monkeypatch):
        _stub_query_searxng(monkeypatch, {"results": _raw_results(20)})
        _stub_fetch_url(monkeypatch)

        result = await search.search_web("query", max_results=1000)

        assert len(_entries(result)) == search.HARD_MAX_RESULTS

    async def test_non_positive_max_results_still_returns_at_least_one(
        self, monkeypatch
    ):
        _stub_query_searxng(monkeypatch, {"results": _raw_results(5)})
        _stub_fetch_url(monkeypatch)

        result = await search.search_web("query", max_results=0)

        assert len(_entries(result)) == 1


class TestRetry:
    async def test_succeeds_on_second_attempt_after_first_fails(self, monkeypatch):
        _stub_query_searxng(
            monkeypatch, ConnectionError("boom"), {"results": _raw_results(1)}
        )
        _stub_fetch_url(monkeypatch)

        result = await search.search_web("query")

        assert "Title 0" in result

    async def test_reports_unavailable_after_both_attempts_fail(self, monkeypatch):
        _stub_query_searxng(
            monkeypatch, ConnectionError("boom"), ConnectionError("boom again")
        )

        result = await search.search_web("query")

        assert result == "web search is unavailable right now"


class TestFetchEnrichment:
    async def test_top_results_get_full_page_content_appended(self, monkeypatch):
        _stub_query_searxng(monkeypatch, {"results": _raw_results(3)})
        called_urls = _stub_fetch_url(monkeypatch)

        result = await search.search_web("query", max_results=3)

        assert called_urls == [
            "https://example0.com/page",
            "https://example1.com/page",
        ]
        assert "FULL CONTENT for https://example0.com/page" in result
        assert "FULL CONTENT for https://example1.com/page" in result
        assert "FULL CONTENT for https://example2.com/page" not in result

    async def test_fetch_errors_are_not_appended_to_the_result(self, monkeypatch):
        _stub_query_searxng(monkeypatch, {"results": _raw_results(1)})

        async def _failing_fetch(url):
            return "couldn't fetch that url"

        monkeypatch.setattr(search, "fetch_url", _failing_fetch)

        result = await search.search_web("query")

        assert "couldn't fetch that url" not in result


class TestEmptyResults:
    async def test_no_results_found_message(self, monkeypatch):
        _stub_query_searxng(monkeypatch, {"results": []})

        result = await search.search_web("query")

        assert result == "no results found"


class TestSchema:
    def test_function_name(self):
        assert search.SCHEMA["function"]["name"] == "search_web"

    def test_max_results_is_optional(self):
        params = search.SCHEMA["function"]["parameters"]
        assert "max_results" in params["properties"]
        assert "max_results" not in params["required"]
