import asyncio
import pytest

from application.modes import get_mode_policy
from tools.search.ledger import SourceLedger
from tools.search.planner import plan_queries
from tools.search.synthesizer import synthesize
from tools.search.pipeline import research, _select_urls, _dedup_results


class DummyResponse:
    def __init__(self, content: str):
        self._content = content

    def get(self, key, default=None):
        if key == "message":
            return {"content": self._content}
        return default


class TestSourceLedger:
    def test_registration_and_sequential_ids(self):
        ledger = SourceLedger()
        s1 = ledger.register("https://a.com", "Title A", "Snippet A")
        s2 = ledger.register("https://b.com", "Title B", "Snippet B")

        assert s1.id == 1
        assert s2.id == 2
        assert len(ledger) == 2

    def test_duplicate_urls_are_deduplicated(self):
        ledger = SourceLedger()
        s1 = ledger.register("https://a.com", "Title A", "Snippet A")
        s2 = ledger.register("https://a.com", "Duplicate", "Snippet A2")

        assert s1.id == s2.id == 1
        assert len(ledger) == 1

    def test_enrich_attaches_full_text(self):
        ledger = SourceLedger()
        s1 = ledger.register("https://a.com", "Title A", "Snippet A")
        ledger.enrich(s1.id, "Full page text")

        assert s1.full_text == "Full page text"
        assert ledger.get(s1.id).full_text == "Full page text"

    def test_to_context_formatting(self):
        ledger = SourceLedger()
        s1 = ledger.register("https://a.com", "Title A", "Snippet A")
        ledger.enrich(s1.id, "Full text here")

        context = ledger.to_context()
        assert "[1] Title A — https://a.com" in context
        assert "Snippet: Snippet A" in context
        assert "Full content: Full text here" in context

    def test_to_footer_with_and_without_cited_ids(self):
        ledger = SourceLedger()
        ledger.register("https://a.com", "Title A", "Snippet A")
        ledger.register("https://b.com", "Title B", "Snippet B")

        footer_all = ledger.to_footer()
        assert "[1] Title A — https://a.com" in footer_all
        assert "[2] Title B — https://b.com" in footer_all

        footer_selected = ledger.to_footer({2})
        assert "[1] Title A" not in footer_selected
        assert "[2] Title B — https://b.com" in footer_selected


class TestQueryPlanner:
    async def test_successful_query_decomposition(self, monkeypatch):
        async def fake_chat(session, messages, tool_schemas, think=False):
            return DummyResponse('{"sub_queries": ["query 1", "query 2"]}')

        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        queries = await plan_queries(None, "complex question")
        assert queries == ["query 1", "query 2"]

    async def test_markdown_fence_stripping(self, monkeypatch):
        async def fake_chat(session, messages, tool_schemas, think=False):
            return DummyResponse('```json\n{"sub_queries": ["fenced 1"]}\n```')

        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        queries = await plan_queries(None, "complex question")
        assert queries == ["fenced 1"]

    async def test_invalid_json_fallback(self, monkeypatch):
        async def fake_chat(session, messages, tool_schemas, think=False):
            return DummyResponse("not valid json")

        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        queries = await plan_queries(None, "original question")
        assert queries == ["original question"]


class TestSynthesizer:
    async def test_empty_ledger_returns_unavailable_message(self):
        ledger = SourceLedger()
        result = await synthesize(None, "some question", ledger)
        assert result == "ไม่สามารถค้นหาข้อมูลได้ในขณะนี้"

    async def test_synthesizes_answer_with_citations_and_footer(self, monkeypatch):
        ledger = SourceLedger()
        ledger.register("https://example.com/item", "Example Item", "Snippet about item")

        async def fake_chat(session, messages, tool_schemas, think=False):
            return DummyResponse("Item details based on [1].")

        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        result = await synthesize(None, "question", ledger)
        assert "Item details based on [1]." in result
        assert "Sources:" in result
        assert "[1] Example Item — https://example.com/item" in result


class TestSelectUrls:
    async def test_selects_all_when_fewer_than_top_n(self):
        results = [
            {"title": "1", "url": "https://1.com", "snippet": "s1"},
            {"title": "2", "url": "https://2.com", "snippet": "s2"},
        ]
        selected = await _select_urls(None, "q", results, top_n=3)
        assert selected == [0, 1]

    async def test_model_selects_specific_indices(self, monkeypatch):
        results = [
            {"title": f"T{i}", "url": f"https://{i}.com", "snippet": f"s{i}"}
            for i in range(5)
        ]

        async def fake_chat(session, messages, tool_schemas, think=False):
            return DummyResponse("[1, 3]")

        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        selected = await _select_urls(None, "q", results, top_n=2)
        assert selected == [1, 3]

    async def test_invalid_selector_response_falls_back_to_top_n(self, monkeypatch):
        results = [
            {"title": f"T{i}", "url": f"https://{i}.com", "snippet": f"s{i}"}
            for i in range(5)
        ]

        async def fake_chat(session, messages, tool_schemas, think=False):
            return DummyResponse("invalid")

        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        selected = await _select_urls(None, "q", results, top_n=3)
        assert selected == [0, 1, 2]


class TestResearchPipelineIntegration:
    async def test_research_end_to_end_flow(self, monkeypatch):
        # Stub search_web_raw
        async def fake_search_raw(query, max_results=None):
            return [
                {
                    "title": f"Result for {query}",
                    "url": f"https://example.com/{query.replace(' ', '_')}",
                    "snippet": f"Snippet for {query}",
                }
            ]

        # Stub fetch_url
        async def fake_fetch(url):
            return f"Full page content of {url}"

        # Stub client.chat for planner, selector, synthesizer
        chat_calls = []

        async def fake_chat(session, messages, tool_schemas, think=False, max_output_tokens=None):
            prompt = messages[0]["content"]
            if "search query planner" in prompt:
                chat_calls.append("plan")
                return DummyResponse('{"sub_queries": ["sub query 1", "sub query 2"]}')
            elif "result selector" in prompt:
                chat_calls.append("select")
                return DummyResponse("[0, 1]")
            elif "research synthesizer" in prompt:
                chat_calls.append("synthesize")
                return DummyResponse("Here is the comparison between sources [1] and [2].")
            return DummyResponse("default")

        from tools.search import pipeline
        monkeypatch.setattr(pipeline, "search_web_raw", fake_search_raw)
        monkeypatch.setattr(pipeline, "fetch_url", fake_fetch)
        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        result = await research("Compare product A and B")

        assert "plan" in chat_calls
        assert "synthesize" in chat_calls
        assert "Here is the comparison between sources [1] and [2]." in result
        assert "Sources:" in result
        assert "https://example.com/sub_query_1" in result
        assert "https://example.com/sub_query_2" in result

    async def test_research_when_no_search_results_found(self, monkeypatch):
        async def fake_search_raw(query, max_results=None):
            return []

        async def fake_chat(session, messages, tool_schemas, think=False, max_output_tokens=None):
            return DummyResponse('{"sub_queries": ["q1"]}')

        from tools.search import pipeline
        monkeypatch.setattr(pipeline, "search_web_raw", fake_search_raw)
        from ai import client
        monkeypatch.setattr(client, "chat", fake_chat)

        result = await research("query with no hits")
        assert result == "ไม่สามารถค้นหาข้อมูลได้ในขณะนี้"


def test_mode_policies():
    agent_policy = get_mode_policy("agent")
    assert "research" in agent_policy.allowed_tools
    assert "search_web" in agent_policy.allowed_tools

    chat_policy = get_mode_policy("chat")
    assert "research" not in chat_policy.allowed_tools
    assert "search_web" in chat_policy.allowed_tools
