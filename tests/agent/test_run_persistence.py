"""Real SQLite run ownership, restart, and Skill selection boundaries."""

import sqlite3

import pytest

from agent.events import AgentEvent
from skills import SkillSelection, builtin_registry
from workflows.storage.runs import SessionBusyError
from workflows.storage.store import JobStore


def session(store):
    user = store.resolve_channel_identity("cli", "local")
    return store.get_or_create_conversation(user.id, "cli", "local")


@pytest.mark.parametrize("status,text", [
    ("completed", "Answer token=private"),
    ("failed", "Request failed."),
    ("cancelled", "Request cancelled."),
])
def test_terminal_result_survives_restart(tmp_path, status, text):
    path = tmp_path / "suto.db"
    first = JobStore(path)
    conversation = session(first)
    run_id = first.begin_agent_run(conversation.id)
    first.start_agent_run(run_id)
    first.finish_agent_run(run_id, status, final_text=text,
                           error=None if status == "completed" else status,
                           usage={"prompt_tokens": 3, "output_tokens": 2, "secret": "bad"})

    second = JobStore(path)
    row = second.get_agent_run(run_id)
    assert row["status"] == status
    assert row["session_id"] == conversation.id
    assert row["created_at"] and row["started_at"] and row["completed_at"]
    assert row["usage"] == {"prompt_tokens": 3, "output_tokens": 2}
    assert "private" not in row["final_text"]
    assert second.recover_interrupted_runs() == 0


def test_two_store_instances_compete_and_dead_owner_recovers(tmp_path):
    path = tmp_path / "suto.db"
    first, second = JobStore(path), JobStore(path)
    conversation = session(first)
    run_id = first.begin_agent_run(conversation.id)
    with pytest.raises(SessionBusyError):
        second.begin_agent_run(conversation.id)
    # A dead PID models a process crash without relying on timing or sleeps.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE agent_runs SET owner_pid=-1 WHERE id=?", (run_id,))
    assert second.recover_interrupted_runs() == 1
    assert second.get_agent_run(run_id)["status"] == "interrupted"
    assert second.get_agent_run(run_id)["completed_at"]
    next_id = second.begin_agent_run(conversation.id)
    assert next_id != run_id
    second.finish_agent_run(next_id, "cancelled")


def test_session_skill_names_survive_restart_and_missing_skill_blocks(tmp_path):
    path = tmp_path / "suto.db"
    first = JobStore(path)
    conversation = session(first)
    selection = SkillSelection(builtin_registry())
    selection.bind(first, conversation.id)
    selection.activate("research")
    second = JobStore(path)
    restored = SkillSelection(builtin_registry())
    restored.bind(second, conversation.id)
    assert restored.require_available() == ("research",)
    second.set_session_skills(conversation.id, ("missing",))
    restored.bind(second, conversation.id)
    with pytest.raises(ValueError, match="unknown skill: missing"):
        restored.require_available()
    restored.deactivate("missing")
    assert second.get_session_skills(conversation.id) == ()


def test_version_13_migrates_without_losing_history_or_trace(tmp_path):
    path = tmp_path / "suto.db"
    first = JobStore(path)
    conversation = session(first)
    first.add_message_batch(conversation.id, conversation.user_id,
                            [{"role": "user", "content": "historical"}])
    first.add_run_event(AgentEvent("old-run", conversation.id, "agent.completed"))
    with sqlite3.connect(path) as db:
        db.executescript("DROP TABLE session_skills; DROP TABLE agent_runs; PRAGMA user_version=13;")
    second = JobStore(path)
    assert second.list_messages(conversation.id)[0].content == "historical"
    assert second.list_run_events("old-run")[0]["event_type"] == "agent.completed"
    assert second.begin_agent_run(conversation.id)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 14
