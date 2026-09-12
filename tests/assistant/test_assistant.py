import ai
import pytest
from assistant import AssistantContext
from automation.storage.store import JobStore
from tests.support.ai_helpers import FakeClientSession


def test_identity_links_channels_and_stores_preferences(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "tui",
        "local",
        display_name="Suto Owner",
        timezone="Asia/Bangkok",
    )

    assert store.resolve_channel_identity("tui", "local").id == user.id
    assert store.resolve_channel_identity(
        "discord",
        "123",
        user_id=user.id,
    ).id == user.id

    store.set_user_preference(user.id, "briefing_time", "08:30")
    assert store.user_preferences(user.id) == {"briefing_time": "08:30"}


def test_conversations_are_persistent_and_isolated(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    first = store.resolve_channel_identity("tui", "one")
    second = store.resolve_channel_identity("tui", "two")
    conversation = store.get_or_create_conversation(first.id, "tui", "main")
    other = store.get_or_create_conversation(second.id, "tui", "main")

    store.add_message(conversation.id, "user", "My project is Suto")
    store.add_message(conversation.id, "assistant", "I will remember that")

    assert store.get_or_create_conversation(first.id, "tui", "main").id == conversation.id
    assert [item["role"] for item in store.conversation_history(conversation.id)] == [
        "user",
        "assistant",
    ]
    assert store.conversation_history(other.id) == []


def test_tasks_and_reminders_are_owned_by_one_user(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity("tui", "owner", timezone="Asia/Bangkok")
    other = store.resolve_channel_identity("tui", "other")

    task = store.create_task(
        owner.id,
        "Call the customer",
        due_at="2026-09-13T10:00:00+07:00",
    )
    reminder = store.create_reminder(
        owner.id,
        "Prepare for the call",
        "2026-09-13T09:30:00+07:00",
        timezone="Asia/Bangkok",
        now="2026-09-12T10:00:00+07:00",
    )

    assert [item.id for item in store.list_tasks(owner.id)] == [task.id]
    assert store.list_tasks(other.id) == []
    assert store.complete_task(other.id, task.id) is None
    assert store.complete_task(owner.id, task.id).status == "completed"
    assert [item.id for item in store.list_reminders(owner.id)] == [reminder.id]
    assert store.cancel_reminder(other.id, reminder.id) is None
    assert store.cancel_reminder(owner.id, reminder.id).status == "cancelled"


def test_due_reminders_are_claimed_once_and_future_reminders_are_left_scheduled(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity("tui", "owner")
    due = store.create_reminder(
        owner.id,
        "Take medicine",
        "2026-09-12T09:00:00+07:00",
        timezone="Asia/Bangkok",
        now="2026-09-12T08:00:00+07:00",
    )
    future = store.create_reminder(
        owner.id,
        "Future reminder",
        "2026-09-12T11:00:00+07:00",
        timezone="Asia/Bangkok",
        now="2026-09-12T08:00:00+07:00",
    )

    claimed = store.claim_due_reminders(
        owner.id,
        now="2026-09-12T10:00:00+07:00",
    )

    assert [item.id for item in claimed] == [due.id]
    assert store.get_reminder(owner.id, due.id).status == "delivered"
    assert store.get_reminder(owner.id, future.id).status == "scheduled"
    assert store.claim_due_reminders(
        owner.id,
        now="2026-09-12T10:00:00+07:00",
    ) == []


def test_python_calculates_relative_and_clock_reminder_times(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity(
        "tui",
        "owner",
        timezone="Asia/Bangkok",
    )

    relative = store.create_relative_reminder(
        owner.id,
        "พักสายตา",
        10,
        timezone="Asia/Bangkok",
        now="2026-09-12T21:00:00+07:00",
    )
    next_clock = store.create_clock_reminder(
        owner.id,
        "กินยา",
        "20:30",
        timezone="Asia/Bangkok",
        now="2026-09-12T21:00:00+07:00",
    )

    assert relative.remind_at == "2026-09-12T21:10:00+07:00"
    assert next_clock.remind_at == "2026-09-13T20:30:00+07:00"


def test_reminder_rejects_past_time(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity("tui", "owner")

    with pytest.raises(ValueError, match="must be in the future"):
        store.create_reminder(
            owner.id,
            "อดีต",
            "2025-01-01T10:00:00+07:00",
            now="2026-09-12T21:00:00+07:00",
        )

    reminder = store.create_relative_reminder(
        owner.id,
        "อนาคต",
        10,
        now="2026-09-12T21:00:00+07:00",
    )
    with pytest.raises(ValueError, match="must be in the future"):
        store.reschedule_reminder(
            owner.id,
            reminder.id,
            "2025-01-01T10:00:00+07:00",
            now="2026-09-12T21:00:00+07:00",
        )


async def test_conversation_history_is_sent_to_the_model(monkeypatch):
    observed = {}

    async def fake_chat(session, messages, tool_schemas, think=False):
        observed["messages"] = messages
        return {"message": {"content": "You said Suto"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    await ai.ask_local_ai(
        "What did I say?",
        mode="chat",
        conversation_history=[
            {"role": "user", "content": "My project is Suto"},
            {"role": "assistant", "content": "Understood"},
        ],
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert [item["content"] for item in observed["messages"][1:]] == [
        "My project is Suto",
        "Understood",
        "What did I say?",
    ]


async def test_agent_can_create_task_for_current_user(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "tui",
        "owner",
        display_name="Suto Owner",
        timezone="Asia/Bangkok",
    )
    store.set_user_preference(user.id, "briefing", "concise")
    conversation = store.get_or_create_conversation(user.id, "tui", "main")
    calls = 0
    observed_tools = set()

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert '"display_name": "Suto Owner"' in messages[0]["content"]
            assert '"timezone": "Asia/Bangkok"' in messages[0]["content"]
            assert '"briefing": "concise"' in messages[0]["content"]
        observed_tools.update(item["function"]["name"] for item in tool_schemas)
        if calls == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "create_task",
                                "arguments": {"title": "Call the customer"},
                            }
                        }
                    ],
                }
            }
        return {"message": {"content": "Task created"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    answer = await ai.ask_local_ai(
        "Add calling the customer to my tasks",
        mode="agent",
        assistant_context=AssistantContext(store, user.id, conversation.id),
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert answer == "Task created"
    assert "create_task" in observed_tools
    assert "apply_workspace_patch" not in observed_tools
    assert [task.title for task in store.list_tasks(user.id)] == ["Call the customer"]


async def test_agent_cannot_claim_uncreated_reminder(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "owner", timezone="Asia/Bangkok")
    conversation = store.get_or_create_conversation(user.id, "tui", "main")

    async def fake_chat(session, messages, tool_schemas, think=False):
        return {"message": {"content": "ตั้งเตือนให้อีก 10 นาทีแล้วครับ"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    result = await ai.execute_local_ai(
        "อีก 10 นาทีเตือนให้กินยา",
        mode="agent",
        assistant_context=AssistantContext(store, user.id, conversation.id),
        reply_language=ai.ReplyLanguage("th", "Thai", "test"),
    )

    assert result.status == "failed"
    assert result.text == "สร้างการแจ้งเตือนไม่สำเร็จ กรุณาลองอีกครั้ง"
    assert store.list_reminders(user.id) == []


async def test_agent_relative_reminder_tool_persists_before_success_reply(
    tmp_path,
    monkeypatch,
):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "owner", timezone="Asia/Bangkok")
    conversation = store.get_or_create_conversation(user.id, "tui", "main")
    calls = 0

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "create_reminder_in",
                                "arguments": {"title": "พักสายตา", "minutes": 10},
                            }
                        }
                    ],
                }
            }
        return {"message": {"content": "สร้างการแจ้งเตือนแล้ว"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    result = await ai.execute_local_ai(
        "อีก 10 นาทีเตือนให้พักสายตา",
        mode="agent",
        assistant_context=AssistantContext(store, user.id, conversation.id),
        reply_language=ai.ReplyLanguage("th", "Thai", "test"),
    )

    assert result.status == "completed"
    assert [item.title for item in store.list_reminders(user.id)] == ["พักสายตา"]
