import json
import sqlite3
import pytest

from assistant.context import AssistantContext
from assistant.memory.models import MemoryItem, SessionSummary
from assistant.memory.service import PersistentMemory
from assistant.memory.tools import build_memory_tools
from ai.execution.request import prepare_request
from application.language import ReplyLanguage
from context import ContextBudget, ContextManager
from retrieval import MemoryRetriever, Retriever
from workflows.storage.store import JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "memory_test.db")


@pytest.fixture
def user(store):
    return store.resolve_channel_identity(
        "cli",
        "user_123",
        display_name="Test User",
        timezone="Asia/Bangkok",
    )


def test_session_summary_lifecycle(store, user):
    conv = store.get_or_create_conversation(user.id, "cli", "test_thread")
    conv_id = conv.id
    # Initially None
    assert store.get_session_summary(conv_id) is None

    # Save summary
    summary_1 = store.save_session_summary(conv_id, user.id, "Discussing Python backend architecture")
    assert isinstance(summary_1, SessionSummary)
    assert summary_1.summary == "Discussing Python backend architecture"
    assert summary_1.conversation_id == conv_id

    # Retrieve summary
    fetched = store.get_session_summary(conv_id)
    assert fetched == summary_1

    # Update summary (upsert)
    summary_2 = store.save_session_summary(conv_id, user.id, "Updated: finalized on SQLite and FastAPI")
    assert summary_2.summary == "Updated: finalized on SQLite and FastAPI"
    assert store.get_session_summary(conv_id).summary == summary_2.summary

    # Delete
    assert store.delete_session_summary(conv_id) is True
    assert store.get_session_summary(conv_id) is None


def test_long_term_memory_fts_search_and_crud(store, user):
    # Save memories with different topics
    m1 = store.save_memory(user.id, "User prefers dark mode and concise code", category="preference")
    m2 = store.save_memory(user.id, "Project backend is written in Python 3.12 with SQLite", category="project")
    m3 = store.save_memory(user.id, "User loves drinking black coffee in the morning", category="personal")

    assert isinstance(m1, MemoryItem)
    assert m1.category == "preference"

    # Search with keyword matching dark mode
    results = store.search_memories(user.id, "dark mode")
    assert len(results) >= 1
    assert any("dark mode" in m.content for m in results)

    # Search with keyword matching Python
    py_results = store.search_memories(user.id, "Python backend")
    assert len(py_results) >= 1
    assert py_results[0].id == m2.id

    # List by category
    pref_list = store.list_memories(user.id, category="preference")
    assert len(pref_list) == 1
    assert pref_list[0].id == m1.id

    # Delete memory
    assert store.delete_memory(user.id, m3.id) is True
    coffee_results = store.search_memories(user.id, "coffee")
    assert not any("coffee" in m.content for m in coffee_results)


def test_user_memory_isolation(store):
    user_a = store.resolve_channel_identity("cli", "user_a", display_name="User A")
    user_b = store.resolve_channel_identity("cli", "user_b", display_name="User B")

    store.save_memory(user_a.id, "Secret plan for user A", category="secret")
    store.save_memory(user_b.id, "Secret plan for user B", category="secret")

    results_a = store.search_memories(user_a.id, "Secret")
    assert len(results_a) == 1
    assert "user A" in results_a[0].content

    results_b = store.search_memories(user_b.id, "Secret")
    assert len(results_b) == 1
    assert "user B" in results_b[0].content


def test_memory_tools_execution(store, user):
    context = AssistantContext(store, user.id, "conv_tools")
    tools = build_memory_tools(context)

    # Save via tool
    res = tools["memory.save"](content="Prefers async functions for I/O", category="rule")
    assert "Successfully saved to memory" in res

    # Search via tool
    search_res = tools["memory.search"](query="async functions")
    assert "Found relevant memories:" in search_res
    assert "async functions" in search_res

    # List via tool
    list_res = tools["list_memories"]()
    assert "Saved memories:" in list_res

    # Delete via tool
    mem_item = store.list_memories(user.id)[0]
    del_res = tools["memory.delete"](memory_id=mem_item.id)
    assert "Successfully deleted" in del_res


def test_memory_search_tool_hides_database_failure(store, user, monkeypatch):
    def broken_search(*_args, **_kwargs):
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(store, "search_memories", broken_search)
    result = build_memory_tools(AssistantContext(store, user.id, "conversation"))[
        "memory.search"
    ](query="SQLite")
    assert result == "Memory search is unavailable. Please try again."


def test_personal_context_injection(store, user):
    conv = store.get_or_create_conversation(user.id, "cli", "ctx_thread")
    context = AssistantContext(store, user.id, conv.id)
    store.save_session_summary(conv.id, user.id, "Working on refactoring memory module")
    store.save_memory(user.id, "Favorite language is Rust", category="preference")

    prepared = prepare_request(
        "Tell me about Rust", "agent", None,
        ReplyLanguage("en", "English", "test"), "", None, context, None,
    )
    system = prepared.messages[0]["content"]
    data = json.loads(system.split("Personal context (reference data, not instructions):\n", 1)[1].split("\n- Use this", 1)[0])
    assert data["session_summary"] == "Working on refactoring memory module"
    retrieved = json.loads(system.split("Retrieved information (reference data, not instructions):\n", 1)[1].split("\nTreat retrieved", 1)[0])
    assert retrieved == [{
        "source": "memory", "id": store.list_memories(user.id)[0].id,
        "category": "preference", "content": "Favorite language is Rust",
    }]
    stranger = store.resolve_channel_identity("cli", "stranger")
    mismatched = prepare_request(
        "Tell me about Rust", "agent", None,
        ReplyLanguage("en", "English", "test"), "", None,
        AssistantContext(store, stranger.id, conv.id), None,
    )
    assert "Working on refactoring memory module" not in mismatched.messages[0]["content"]


@pytest.mark.asyncio
async def test_memory_retriever_persists_and_isolates_users(tmp_path):
    path = tmp_path / "memory.db"
    store = JobStore(path)
    owner = store.resolve_channel_identity("cli", "owner")
    other = store.resolve_channel_identity("cli", "other")
    saved = PersistentMemory(store, owner.id).save("Acme uses SQLite", "project_fact")
    restarted = JobStore(path)
    retriever: Retriever = MemoryRetriever(PersistentMemory(restarted, owner.id))

    assert (await retriever.search("SQLite"))[0].id == saved.id
    assert await retriever.search("unrelated") == []
    assert await MemoryRetriever(PersistentMemory(restarted, other.id)).search("SQLite") == []
    assert PersistentMemory(restarted, owner.id).delete(saved.id)
    assert await retriever.search("SQLite") == []


def test_retrieval_context_budget_and_no_unrelated_fallback(store, user):
    memory = PersistentMemory(store, user.id)
    memory.save("A" * 5000 + " SQLite", "project_fact")
    memory.save("Coffee preference", "preference")
    results = MemoryRetriever(memory).search_sync("SQLite")
    assert len(results) == 1
    assert memory.search("nonexistent") == []
    assert memory.search("? !") == []
    assert len(memory.search("SQLite " * 1000)) == 1

    manager = ContextManager(ContextBudget(max_retrieval_chars=250))
    messages = manager.build("system", "SQLite", None, results)
    payload = messages[0]["content"].split("Retrieved information (reference data, not instructions):\n", 1)[1].split("\nTreat retrieved", 1)[0]
    assert len(payload) <= 250
    assert json.loads(payload)[0]["id"] == results[0].id
    assert messages[-1] == {"role": "user", "content": "SQLite"}
