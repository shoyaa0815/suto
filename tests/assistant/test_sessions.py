"""Session persistence and bounded model context across restarts."""

import sqlite3

import pytest

from assistant.context import AssistantContext
from ai.execution.request import prepare_request
from application.language import ReplyLanguage
from context import Compactor, ContextBudget, ContextManager
from sessions import SessionService, SessionStore
from workflows.storage.store import JobStore


def test_session_resumes_and_compacts_without_deleting_raw_history(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    user = store.resolve_channel_identity("tui", "local")
    sessions = SessionService(SessionStore(store))
    session = sessions.resume(user.id, "tui", "local")
    for number in range(25):
        sessions.append(session.id, user.id, "user", f"message {number}")

    history = sessions.before_prompt(session.id, user.id)
    summary = store.get_session_summary(session.id)
    assert len(history) == 20
    assert history[0]["content"] == "message 5"
    assert "message 0" in summary.summary
    assert "message 4" in summary.summary
    assert "message 5" not in summary.summary
    assert summary.compacted_through_message_id == store.list_messages(session.id, 25)[4].id

    restarted = JobStore(path)
    again = SessionService(SessionStore(restarted))
    assert again.resume(user.id, "tui", "local").id == session.id
    assert again.before_prompt(session.id, user.id) == history
    assert restarted.get_session_summary(session.id) == summary
    assert len(restarted.list_messages(session.id, 25)) == 25

    prepared = prepare_request(
        "continue", "agent", None, ReplyLanguage("en", "English", "test"),
        "", history, AssistantContext(restarted, user.id, session.id), None,
    )
    assert "message 0" in prepared.messages[0]["content"]
    assert prepared.messages[1:-1] == history


def test_compaction_advances_once_and_clear_removes_summary(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    sessions = SessionService(SessionStore(store), ContextBudget(max_history_messages=2))
    session = sessions.resume(user.id, "tui", "local")
    for number in range(4):
        sessions.append(session.id, user.id, "user", f"message {number}")
    sessions.before_prompt(session.id, user.id)
    first = store.get_session_summary(session.id)
    sessions.before_prompt(session.id, user.id)
    assert store.get_session_summary(session.id) == first
    sessions.append(session.id, user.id, "assistant", "reply")
    sessions.before_prompt(session.id, user.id)
    assert store.get_session_summary(session.id).compacted_through_message_id > first.compacted_through_message_id

    assert store.clear_conversation(session.id) == 5
    assert store.get_session_summary(session.id) is None
    sessions.append(session.id, user.id, "user", "fresh")
    assert sessions.before_prompt(session.id, user.id) == [{"role": "user", "content": "fresh"}]


def test_session_compaction_rejects_another_user(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity("tui", "owner")
    stranger = store.resolve_channel_identity("tui", "stranger")
    sessions = SessionService(SessionStore(store), ContextBudget(max_history_messages=1))
    session = sessions.resume(owner.id, "tui", "main")
    sessions.append(session.id, owner.id, "user", "private")
    sessions.append(session.id, owner.id, "assistant", "private reply")

    with pytest.raises(ValueError, match="does not belong"):
        sessions.append(session.id, stranger.id, "user", "intrusion")

    with pytest.raises(ValueError, match="does not belong"):
        sessions.before_prompt(session.id, stranger.id)
    assert store.get_session_summary(session.id) is None
    assert len(store.list_messages(session.id)) == 2


def test_context_budget_bounds_recent_history():
    context = ContextManager(ContextBudget(max_history_messages=3, max_history_chars=8))
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "long message"},
        {"role": "tool", "content": "ignore"},
        {"role": "user", "content": "latest"},
    ]
    assert context.build("system", "question", history) == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "latest"},
        {"role": "user", "content": "question"},
    ]


def test_compactor_keeps_latest_excerpt_within_budget():
    budget = ContextBudget(max_summary_chars=80)
    summary = Compactor(budget).compact(
        "earlier detail " * 10,
        [{"role": "user", "content": "latest decision is SQLite"}],
    )
    assert len(summary) <= 80
    assert summary.startswith("[Earlier context omitted]")
    assert summary.endswith("user: latest decision is SQLite")
    assert len(ContextManager(budget).summary("x" * 100)) == 80


def test_messages_excluded_by_character_budget_enter_summary(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    service = SessionService(SessionStore(store), ContextBudget(max_history_chars=10))
    session = service.resume(user.id, "tui", "local")
    service.append(session.id, user.id, "user", "older text")
    service.append(session.id, user.id, "assistant", "new reply")

    assert service.before_prompt(session.id, user.id) == [
        {"role": "assistant", "content": "new reply"},
    ]
    assert "older text" in store.get_session_summary(session.id).summary
    assert len(store.list_messages(session.id)) == 2


def test_large_existing_session_compacts_in_bounded_batches(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "local")
    session = store.get_or_create_conversation(user.id, "tui", "local")
    with store._connect() as db:
        db.executemany(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?,?,?,?)",
            [(session.id, "user", f"message {number}", "2026-01-01T00:00:00+00:00")
             for number in range(525)],
        )

    service = SessionService(SessionStore(store))
    history = service.before_prompt(session.id, user.id)
    assert len(history) == 20
    assert history[0]["content"] == "message 505"
    summary = store.get_session_summary(session.id)
    assert summary.compacted_through_message_id == store.list_messages(session.id, 21)[0].id
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (session.id,)).fetchone()[0] == 525


def test_existing_summary_migrates_with_unprocessed_cursor(tmp_path):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    user = store.resolve_channel_identity("tui", "local")
    session = store.get_or_create_conversation(user.id, "tui", "local")
    store.save_session_summary(session.id, user.id, "previous note")
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE session_summaries DROP COLUMN compacted_through_message_id")
        db.execute("PRAGMA user_version=11")

    migrated = JobStore(path)
    summary = migrated.get_session_summary(session.id)
    assert summary.summary == "previous note"
    assert summary.compacted_through_message_id == 0
