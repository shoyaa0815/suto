import sqlite3
import threading
from contextlib import contextmanager

import ai
import pytest
from assistant import AssistantContext, DeliveryTargetContext
from assistant.tasks.tools import build_task_tools
from ai.tooling.assembly import build_runtime_tools
from workflows.storage.store import JobStore
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


def test_personal_datetime_tool_uses_the_users_timezone(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "tui", "local", timezone="Asia/Bangkok"
    )
    observed = []

    def fake_datetime(timezone=None):
        observed.append(timezone)
        return "local time"

    monkeypatch.setattr("ai.tooling.assembly.get_current_datetime", fake_datetime)
    tools = build_runtime_tools(
        session=object(),
        allowed_tools=frozenset({"get_current_datetime"}),
        attachments={},
        assistant_context=AssistantContext(store, user.id, "conversation"),
        execution_context=None,
        change_event_callback=None,
        guard=object(),
        progress=object(),
    )

    assert tools["get_current_datetime"]() == "local time"
    assert observed == ["Asia/Bangkok"]


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


def test_clear_conversation_removes_only_its_messages(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    first = store.resolve_channel_identity("tui", "one")
    second = store.resolve_channel_identity("tui", "two")
    current = store.get_or_create_conversation(first.id, "tui", "main")
    other = store.get_or_create_conversation(second.id, "tui", "main")
    store.add_message(current.id, "user", "forget this")
    store.add_message(other.id, "user", "keep this")

    assert store.clear_conversation(current.id) == 1
    assert store.conversation_history(current.id) == []
    assert store.conversation_history(other.id) == [
        {"role": "user", "content": "keep this"}
    ]


def test_reset_conversations_removes_every_saved_conversation(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    first = store.resolve_channel_identity("tui", "one")
    second = store.resolve_channel_identity("discord", "two")
    current = store.get_or_create_conversation(first.id, "tui", "main")
    other = store.get_or_create_conversation(second.id, "discord", "channel")
    store.add_message(current.id, "user", "first message")
    store.add_message(other.id, "assistant", "second message")

    assert store.reset_conversations() == 2
    assert store.conversation_history(current.id) == []
    assert store.conversation_history(other.id) == []
    assert store.get_or_create_conversation(first.id, "tui", "main").id != current.id


def test_reset_user_conversations_preserves_other_users(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    first = store.resolve_channel_identity("discord", "one")
    second = store.resolve_channel_identity("discord", "two")
    first_dm = store.get_or_create_conversation(first.id, "discord", "one")
    first_guild = store.get_or_create_conversation(first.id, "discord", "shared")
    second_guild = store.get_or_create_conversation(second.id, "discord", "shared")
    store.add_message(first_dm.id, "user", "delete me")
    store.add_message(first_guild.id, "user", "delete me too")
    store.add_message(second_guild.id, "user", "keep me")

    assert store.reset_user_conversations(first.id) == 2
    assert store.conversation_history(first_dm.id) == []
    assert store.conversation_history(first_guild.id) == []
    assert store.conversation_history(second_guild.id) == [
        {"role": "user", "content": "keep me"}
    ]


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


def test_cancel_reminder_by_title_waits_for_writer_before_checking_duplicates(
    tmp_path, monkeypatch
):
    path = tmp_path / "suto.db"
    store = JobStore(path)
    user = store.resolve_channel_identity("tui", "local")
    first = store.create_reminder(
        user.id,
        "same",
        "2099-01-01T01:00:00+00:00",
    )
    writer = sqlite3.connect(path, timeout=10)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO reminders(id,user_id,title,remind_at,timezone,status,"
        "channel_identity_id,created_at,updated_at,delivery_target_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "rem_concurrent",
            user.id,
            "same",
            "2099-01-01T02:00:00+00:00",
            "UTC",
            "scheduled",
            None,
            "2026-09-15T00:00:00+00:00",
            "2026-09-15T00:00:00+00:00",
            None,
        ),
    )

    statement_started = threading.Event()
    original_connect = store._connect

    @contextmanager
    def traced_connect():
        with original_connect() as db:
            db.set_trace_callback(
                lambda sql: statement_started.set()
                if sql.startswith(("BEGIN IMMEDIATE", "SELECT * FROM reminders"))
                else None
            )
            yield db

    monkeypatch.setattr(store, "_connect", traced_connect)
    outcome = {}

    def cancel():
        outcome["value"] = store.cancel_reminder_by_title(user.id, "same")

    worker = threading.Thread(target=cancel)
    worker.start()
    assert statement_started.wait(timeout=2)
    writer.commit()
    writer.close()
    worker.join(timeout=2)

    assert not worker.is_alive()
    removed, matches = outcome["value"]
    assert removed is None
    assert len(matches) == 2
    assert store.get_reminder(user.id, first.id).status == "scheduled"
    assert store.get_reminder(user.id, "rem_concurrent").status == "scheduled"


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


def test_discord_reminder_delivery_is_persistent_and_completed_after_send(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity("discord", "10")
    target = store.get_or_create_delivery_target(
        owner.id,
        "discord",
        "100",
        "guild_channel",
        "#team",
        guild_id="50",
        requester_id="10",
    )
    reminder = store.create_reminder(
        owner.id,
        "Standup",
        "2026-09-13T09:00:00+07:00",
        timezone="Asia/Bangkok",
        delivery_target_id=target.id,
        now="2026-09-13T08:00:00+07:00",
    )

    assert store.claim_due_reminders(
        owner.id, now="2026-09-13T10:00:00+07:00"
    ) == []
    claimed = store.claim_due_reminder_deliveries(
        "discord", now="2026-09-13T10:00:00+07:00"
    )

    assert [item.reminder_id for item in claimed] == [reminder.id]
    assert claimed[0].destination_id == "100"
    assert store.get_reminder(owner.id, reminder.id).status == "scheduled"
    assert store.complete_reminder_delivery(reminder.id) is True
    assert store.get_reminder(owner.id, reminder.id).status == "delivered"


def test_claimed_discord_reminder_is_revalidated_before_delivery(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity("discord", "10")
    target = store.get_or_create_delivery_target(
        owner.id, "discord", "10", "dm", "DM", requester_id="10"
    )
    reminder = store.create_reminder(
        owner.id,
        "Standup",
        "2026-09-13T09:00:00+07:00",
        timezone="Asia/Bangkok",
        delivery_target_id=target.id,
        now="2026-09-13T08:00:00+07:00",
    )
    delivery = store.claim_due_reminder_deliveries(
        "discord", now="2026-09-13T09:00:00+07:00"
    )[0]

    store.cancel_reminder(owner.id, reminder.id)

    assert store.reminder_delivery_is_current(delivery) is False


def test_discord_task_tool_rejects_unavailable_delivery_channel(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity("discord", "10")
    conversation = store.get_or_create_conversation(owner.id, "discord", "100")
    dm = DeliveryTargetContext("discord", "10", "dm", "DM", requester_id="10")
    channel = DeliveryTargetContext(
        "discord", "100", "guild_channel", "#team", "50", "10"
    )
    handlers = build_task_tools(
        AssistantContext(
            store,
            owner.id,
            conversation.id,
            default_delivery_target=dm,
            current_delivery_target=channel,
            available_delivery_targets=(channel,),
        )
    )

    handlers["create_reminder_in"]("Standup", 10, channel_id="100")
    reminder = store.list_reminders(owner.id)[0]
    target = store.get_delivery_target(owner.id, reminder.delivery_target_id)
    assert target.destination_id == "100"

    with pytest.raises(ValueError, match="unavailable or not permitted"):
        handlers["create_reminder_in"]("Secret", 10, channel_id="999")


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


def test_multiple_clock_reminders_are_created_atomically(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    owner = store.resolve_channel_identity(
        "tui", "owner", timezone="Asia/Bangkok"
    )

    reminders = store.create_clock_reminders(
        owner.id,
        "กินยา",
        ["09:00", "18:00"],
        timezone="Asia/Bangkok",
        now="2026-09-13T10:00:00+07:00",
    )

    assert [item.remind_at for item in reminders] == [
        "2026-09-14T09:00:00+07:00",
        "2026-09-13T18:00:00+07:00",
    ]
    with pytest.raises(ValueError, match="HH:MM"):
        store.create_clock_reminders(
            owner.id,
            "กินยา",
            ["07:00", "not-a-time"],
            timezone="Asia/Bangkok",
        )
    assert {item.id for item in store.list_reminders(owner.id)} == {
        item.id for item in reminders
    }


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
    store.set_user_preference(user.id, "briefing_time", "08:30")
    store.set_user_preference(
        user.id,
        "briefing_delivery_target_id",
        "target_old",
    )
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
            assert "briefing_time" not in messages[0]["content"]
            assert "briefing_delivery_target_id" not in messages[0]["content"]
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


async def test_agent_multiple_clock_reminders_persist_before_success_reply(
    tmp_path, monkeypatch
):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("tui", "owner", timezone="Asia/Bangkok")
    conversation = store.get_or_create_conversation(user.id, "tui", "main")
    calls = 0

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "create_reminders_at" in {
                schema["function"]["name"] for schema in tool_schemas
            }
            assert "create_reminders_at once" in messages[0]["content"]
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "create_reminders_at",
                                "arguments": {
                                    "title": "กินยา",
                                    "times": ["09:00", "18:00"],
                                },
                            }
                        }
                    ],
                }
            }
        return {"message": {"content": "สร้างการแจ้งเตือนแล้ว 2 รายการ"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    result = await ai.execute_local_ai(
        "เตือนให้กินยา 09:00 และ 18:00",
        mode="agent",
        assistant_context=AssistantContext(store, user.id, conversation.id),
        reply_language=ai.ReplyLanguage("th", "Thai", "test"),
    )

    assert result.status == "completed"
    reminders = store.list_reminders(user.id)
    assert len(reminders) == 2
    assert {item.remind_at[11:16] for item in reminders} == {"09:00", "18:00"}
