from types import SimpleNamespace

import pytest

import interfaces.discord.bot as discord_bot
from assistant import DeliveryTargetContext
from interfaces.discord.bot import (
    _discord_delivery_context,
    _without_bot_mention,
    deliver_discord_notifications_once,
    handle_personal_command,
    should_process_message,
)
from workflows.storage.store import JobStore


def _message(*, guild, mentions=(), author_bot=False):
    author = SimpleNamespace(id=10, bot=author_bot, display_name="Owner")
    return SimpleNamespace(author=author, guild=guild, mentions=list(mentions))


def test_guild_message_requires_direct_bot_mention():
    bot = object()
    guild = object()

    assert should_process_message(_message(guild=guild), bot) is False
    assert should_process_message(_message(guild=guild, mentions=[bot]), bot) is True


def test_dm_does_not_require_mention_and_bot_messages_are_ignored():
    assert should_process_message(_message(guild=None), object()) is True
    assert should_process_message(
        _message(guild=None, author_bot=True), object()
    ) is False


def test_bot_mention_is_removed_before_sending_prompt_to_ai():
    assert _without_bot_mention("<@123> เตือนห้องนี้", 123) == "เตือนห้องนี้"
    assert _without_bot_mention("<@!123> hello", 123) == "hello"


class _Channel:
    def __init__(self, channel_id, name, bot_permissions, user_permissions):
        self.id = channel_id
        self.name = name
        self.category = None
        self._bot_permissions = bot_permissions
        self._user_permissions = user_permissions

    def permissions_for(self, member):
        return (
            self._bot_permissions
            if getattr(member, "is_bot_member", False)
            else self._user_permissions
        )


def _permissions(*, view=True, send=True):
    return SimpleNamespace(view_channel=view, send_messages=send)


def test_delivery_context_only_exposes_channels_both_sides_can_use():
    bot_member = SimpleNamespace(is_bot_member=True)
    usable = _Channel(100, "team", _permissions(), _permissions())
    bot_cannot_send = _Channel(
        200, "readonly", _permissions(send=False), _permissions()
    )
    user_cannot_view = _Channel(
        300, "secret", _permissions(), _permissions(view=False)
    )
    guild = SimpleNamespace(
        id=50,
        me=bot_member,
        text_channels=[usable, bot_cannot_send, user_cannot_view],
    )
    message = _message(guild=guild)
    message.channel = usable

    default, current, available = _discord_delivery_context(message)

    assert default.destination_type == "dm"
    assert default.destination_id == "10"
    assert current.destination_id == "100"
    assert [item.destination_id for item in available] == ["100"]


def _discord_user(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "discord",
        "10",
        display_name="Owner",
        timezone="Asia/Bangkok",
        locale="th",
    )
    target = DeliveryTargetContext(
        "discord",
        "10",
        "dm",
        "DM with Owner",
        requester_id="10",
    )
    return store, user, target


def test_discord_noti_command_lists_and_cancels_reminders(tmp_path, monkeypatch):
    store, user, target = _discord_user(tmp_path)
    monkeypatch.setattr(discord_bot, "active_mode", "agent")
    reminder = store.create_reminder(
        user.id,
        "กินยา",
        "2099-09-13T09:00:00+07:00",
        timezone="Asia/Bangkok",
    )

    listing = handle_personal_command(store, user, "/noti", target)
    assert reminder.id in listing
    assert "กินยา" in listing
    assert handle_personal_command(
        store, user, f"/noti del {reminder.id}", target
    ) == f"Reminder removed: {reminder.id}"
    assert handle_personal_command(store, user, "/noti", target) == (
        "No pending reminders."
    )


def test_discord_brief_command_manages_dm_schedule(tmp_path, monkeypatch):
    store, user, target = _discord_user(tmp_path)
    monkeypatch.setattr(discord_bot, "active_mode", "agent")

    result = handle_personal_command(store, user, "/brief at 08:30", target)
    preferences = store.user_preferences(user.id)

    assert "Discord DM" in result
    assert preferences["briefing_time"] == "08:30"
    delivery_target = store.get_delivery_target(
        user.id, preferences["briefing_delivery_target_id"]
    )
    assert delivery_target.destination_id == "10"
    assert "08:30" in handle_personal_command(
        store, user, "/brief status", target
    )
    assert handle_personal_command(store, user, "/brief off", target) == (
        "Daily briefing disabled."
    )
    assert store.user_preferences(user.id) == {}


@pytest.mark.asyncio
async def test_discord_scheduled_briefing_is_sent_once(tmp_path, monkeypatch):
    store, user, target_context = _discord_user(tmp_path)
    target = store.get_or_create_delivery_target(
        user.id,
        target_context.platform,
        target_context.destination_id,
        target_context.destination_type,
        target_context.display_name,
        requester_id=target_context.requester_id,
    )
    store.set_user_preference(user.id, "briefing_time", "08:30")
    store.set_user_preference(user.id, "briefing_delivery_target_id", target.id)
    store.create_task(
        user.id,
        "ส่งรายงาน",
        due_at="2026-09-13T10:00:00+07:00",
    )
    sent = []

    async def fake_send(delivery, content):
        sent.append((delivery.destination_id, content))

    monkeypatch.setattr(discord_bot, "_send_to_target", fake_send)
    now = "2026-09-13T08:30:00+07:00"

    await deliver_discord_notifications_once(store, now=now)
    await deliver_discord_notifications_once(store, now=now)

    assert len(sent) == 1
    assert sent[0][0] == "10"
    assert "สรุปประจำวัน" in sent[0][1]
    assert "ส่งรายงาน" in sent[0][1]
