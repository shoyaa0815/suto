from datetime import datetime

from assistant.briefing import (
    briefing_schedule_status,
    build_daily_briefing,
    disable_daily_briefing,
    set_daily_briefing_time,
)
from assistant.context import AssistantContext, DeliveryTargetContext
from assistant.tasks.tools import build_task_tools
from workflows.storage.store import JobStore
from interfaces.tui.operations import claim_due_daily_briefing


def _store_with_user(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "tui",
        "local",
        timezone="Asia/Bangkok",
        locale="th",
    )
    return store, user


def test_daily_briefing_prioritizes_overdue_and_today_items(tmp_path):
    store, user = _store_with_user(tmp_path)
    store.create_task(
        user.id,
        "ส่งใบเสนอราคา",
        due_at="2026-09-13T08:00:00+07:00",
    )
    store.create_task(
        user.id,
        "ประชุมทีม",
        due_at="2026-09-13T14:00:00+07:00",
    )
    store.create_task(user.id, "อ่านเอกสาร")
    store.create_reminder(
        user.id,
        "เตรียมเข้าประชุม",
        "2026-09-13T13:45:00+07:00",
        timezone="Asia/Bangkok",
        now="2026-09-12T10:00:00+07:00",
    )

    result = build_daily_briefing(
        store,
        user.id,
        now=datetime.fromisoformat("2026-09-13T09:00:00+07:00"),
    )

    assert "งานเลยกำหนด (1)" in result
    assert "ส่งใบเสนอราคา — 08:00" in result
    assert "งานวันนี้ (1)" in result
    assert "ประชุมทีม — 14:00" in result
    assert "13:45 เตรียมเข้าประชุม" in result
    assert "งานที่ยังไม่กำหนดเวลา: 1 งาน" in result
    assert "แนะนำให้เริ่มจาก: ส่งใบเสนอราคา" in result


def test_daily_briefing_schedule_can_be_configured_and_disabled(tmp_path):
    store, user = _store_with_user(tmp_path)

    assert briefing_schedule_status(store, user.id) is None
    assert set_daily_briefing_time(store, user.id, "08:30") == "08:30"
    assert briefing_schedule_status(store, user.id) == "08:30"
    assert disable_daily_briefing(store, user.id) is True
    assert briefing_schedule_status(store, user.id) is None


def test_agent_briefing_tools_configure_and_disable_schedule(tmp_path):
    store, user = _store_with_user(tmp_path)
    conversation = store.get_or_create_conversation(user.id, "tui", "local")
    handlers = build_task_tools(AssistantContext(store, user.id, conversation.id))

    assert "08:30" in handlers["set_daily_briefing"]("08:30")
    assert briefing_schedule_status(store, user.id) == "08:30"
    assert handlers["disable_daily_briefing"]() == "daily briefing disabled"
    assert briefing_schedule_status(store, user.id) is None


def test_agent_briefing_tool_binds_external_default_target(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "discord", "10", timezone="Asia/Bangkok"
    )
    conversation = store.get_or_create_conversation(user.id, "discord", "100")
    target = DeliveryTargetContext(
        "discord", "10", "dm", "DM", requester_id="10"
    )
    handlers = build_task_tools(
        AssistantContext(
            store,
            user.id,
            conversation.id,
            default_delivery_target=target,
        )
    )

    handlers["set_daily_briefing"]("08:30")

    preferences = store.user_preferences(user.id)
    persisted = store.get_delivery_target(
        user.id, preferences["briefing_delivery_target_id"]
    )
    assert persisted.platform == "discord"
    assert persisted.destination_id == "10"


def test_external_briefing_claim_revalidates_changed_schedule(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "discord", "10", timezone="Asia/Bangkok"
    )
    target = store.get_or_create_delivery_target(
        user.id, "discord", "10", "dm", "DM", requester_id="10"
    )
    store.set_user_preference(user.id, "briefing_time", "08:30")
    store.set_user_preference(user.id, "briefing_delivery_target_id", target.id)
    now = "2026-09-13T08:30:00+07:00"

    delivery = store.claim_due_briefing_deliveries("discord", now=now)[0]
    store.set_user_preference(user.id, "briefing_time", "09:00")

    assert store.briefing_delivery_is_current(delivery, now=now) is False


def test_external_briefing_delivery_retries_then_completes(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "discord", "10", timezone="Asia/Bangkok"
    )
    target = store.get_or_create_delivery_target(
        user.id, "discord", "10", "dm", "DM", requester_id="10"
    )
    store.configure_external_briefing(user.id, "08:30", target.id)
    due = "2026-09-13T08:30:00+07:00"
    before_retry = "2026-09-13T08:30:29+07:00"
    retry = "2026-09-13T08:30:30+07:00"

    first = store.claim_due_briefing_deliveries("discord", now=due)[0]
    assert first.attempt_count == 1
    assert store.fail_briefing_delivery(
        user.id, first.local_date, "temporary", now=due
    ) == "retrying"
    assert store.claim_due_briefing_deliveries(
        "discord", now=before_retry
    ) == []
    second = store.claim_due_briefing_deliveries("discord", now=retry)[0]
    assert second.attempt_count == 2
    assert store.complete_briefing_delivery(user.id, second.local_date) is True
    assert store.claim_due_briefing_deliveries("discord", now=retry) == []


def test_scheduled_briefing_is_delivered_only_once_per_local_date(tmp_path):
    store, user = _store_with_user(tmp_path)
    set_daily_briefing_time(store, user.id, "08:30")

    before = datetime.fromisoformat("2026-09-13T08:29:00+07:00")
    due = datetime.fromisoformat("2026-09-13T08:30:00+07:00")
    assert claim_due_daily_briefing(store, user.id, now=before) is None
    assert "สรุปประจำวัน" in claim_due_daily_briefing(store, user.id, now=due)
    assert claim_due_daily_briefing(store, user.id, now=due) is None

    tomorrow = datetime.fromisoformat("2026-09-14T08:30:00+07:00")
    assert "2026-09-14" in claim_due_daily_briefing(store, user.id, now=tomorrow)
