from types import SimpleNamespace

import pytest

import interfaces.discord.bot as discord_bot
from application.configuration import ProfileSettings
from assistant import DeliveryTargetContext
from interfaces.discord.bot import (
    _discord_delivery_context,
    _resolve_discord_user,
    _send_to_target,
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


def test_discord_identity_preserves_existing_user_profile(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    existing = store.resolve_channel_identity(
        "discord",
        "10",
        display_name="Old",
        timezone="UTC",
        locale="en",
    )
    message = _message(guild=None)

    user = _resolve_discord_user(
        store,
        message,
        ProfileSettings(
            timezone="Asia/Bangkok",
            locale="th",
            display_name="Suto Owner",
        ),
    )

    assert user.id == existing.id
    assert user.display_name == "Old"
    assert user.timezone == "UTC"
    assert user.locale == "en"


def test_new_discord_identity_uses_author_name_and_profile_defaults(tmp_path):
    store = JobStore(tmp_path / "suto.db")

    user = _resolve_discord_user(
        store,
        _message(guild=None),
        ProfileSettings(
            timezone="Asia/Bangkok",
            locale="th",
            display_name="Suto Owner",
        ),
    )

    assert user.display_name == "Owner"
    assert user.timezone == "Asia/Bangkok"
    assert user.locale == "th"


def test_discord_rejects_home_mode_before_connecting():
    with pytest.raises(ValueError, match="only in the plain terminal"):
        discord_bot.run("home")


def test_discord_startup_does_not_overwrite_existing_identity(
    tmp_path,
    monkeypatch,
):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity(
        "discord",
        "10",
        display_name="Old",
        timezone="UTC",
        locale="en",
    )
    (tmp_path / "config.yaml").write_text(
        "version: 1\nprofile:\n  timezone: Asia/Bangkok\n"
        "  locale: th\n  display_name: Suto Owner\n",
        encoding="utf-8",
    )
    started = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setattr(discord_bot, "assistant_store", store)
    monkeypatch.setattr(discord_bot, "configured_profile", None)
    monkeypatch.setattr(discord_bot.client, "run", started.append)

    discord_bot.run("agent")

    unchanged = store.get_user(user.id)
    assert unchanged.display_name == "Old"
    assert unchanged.timezone == "UTC"
    assert unchanged.locale == "en"
    assert started == ["test-token"]


def test_discord_retries_the_known_initial_gateway_reconnect_bug(
    tmp_path, monkeypatch
):
    (tmp_path / "config.yaml").write_text(
        "version: 1\nprofile:\n  timezone: Asia/Bangkok\n"
        "  locale: th\n  display_name: Suto Owner\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(token):
        calls.append(token)
        if len(calls) == 1:
            raise AttributeError("'NoneType' object has no attribute 'sequence'")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setattr(discord_bot, "configured_profile", None)
    monkeypatch.setattr(discord_bot.client, "run", fake_run)
    monkeypatch.setattr(discord_bot.client, "clear", lambda: calls.append("clear"))
    monkeypatch.setattr(discord_bot.client.http, "connector", object())
    monkeypatch.setattr(discord_bot.time, "sleep", lambda seconds: calls.append(seconds))

    discord_bot.run("agent")

    assert calls == ["test-token", "clear", discord_bot.DISCORD_RETRY_BASE_SECONDS, "test-token"]
    assert discord_bot.client.http.connector is discord_bot.discord.utils.MISSING


def test_discord_retries_server_error_with_exponential_backoff(
    tmp_path, monkeypatch
):
    (tmp_path / "config.yaml").write_text(
        "version: 1\nprofile:\n  timezone: Asia/Bangkok\n"
        "  locale: th\n  display_name: Suto Owner\n",
        encoding="utf-8",
    )
    calls = []

    class _ServerError(Exception):
        status = 500

    def fake_run(token):
        calls.append(token)
        if calls.count("test-token") < 3:
            raise _ServerError()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setattr(discord_bot, "configured_profile", None)
    monkeypatch.setattr(discord_bot.discord, "DiscordServerError", _ServerError)
    monkeypatch.setattr(discord_bot.client, "run", fake_run)
    monkeypatch.setattr(discord_bot.client, "clear", lambda: calls.append("clear"))
    monkeypatch.setattr(discord_bot.client.http, "connector", object())
    monkeypatch.setattr(discord_bot.time, "sleep", lambda seconds: calls.append(seconds))

    discord_bot.run("agent")

    assert calls == ["test-token", "clear", 5, "test-token", "clear", 10, "test-token"]


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
    user_cannot_send = _Channel(
        400, "announcements", _permissions(), _permissions(send=False)
    )
    guild = SimpleNamespace(
        id=50,
        me=bot_member,
        text_channels=[usable, bot_cannot_send, user_cannot_view, user_cannot_send],
    )
    message = _message(guild=guild)
    message.channel = usable

    default, current, available = _discord_delivery_context(message)

    assert default.destination_type == "dm"
    assert default.destination_id == "10"
    assert current.destination_id == "100"
    assert [item.destination_id for item in available] == ["100"]


@pytest.mark.asyncio
async def test_channel_delivery_rechecks_requester_send_permission(monkeypatch):
    bot_member = SimpleNamespace(is_bot_member=True)
    requester = SimpleNamespace(id=10)
    requester_permissions = _permissions()
    channel = _Channel(
        100,
        "announcements",
        _permissions(),
        requester_permissions,
    )
    channel.guild = SimpleNamespace(
        id=50,
        me=bot_member,
        get_member=lambda member_id: requester if member_id == 10 else None,
    )
    sent = []

    async def send(content, **options):
        sent.append((content, options))

    channel.send = send
    fake_client = SimpleNamespace(
        get_channel=lambda channel_id: channel if channel_id == 100 else None,
    )
    monkeypatch.setattr(discord_bot, "client", fake_client)
    delivery = SimpleNamespace(
        destination_type="guild_channel",
        destination_id="100",
        guild_id="50",
        requester_id="10",
    )

    await _send_to_target(delivery, "allowed reminder")
    assert [content for content, _ in sent] == ["allowed reminder"]

    sent.clear()
    requester_permissions.send_messages = False
    with pytest.raises(PermissionError, match="view or send"):
        await _send_to_target(delivery, "private reminder")

    assert sent == []

    delivery.requester_id = None
    with pytest.raises(PermissionError, match="identity is unavailable"):
        await _send_to_target(delivery, "private reminder")

    assert sent == []


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


def test_discord_notification_lists_and_cancels_one_reminder_by_name(
    tmp_path, monkeypatch
):
    store, user, target = _discord_user(tmp_path)
    monkeypatch.setattr(discord_bot, "active_mode", "agent")
    conversation = store.get_or_create_conversation(user.id, "discord", "10")
    reminder = store.create_reminder(
        user.id,
        "กินยา",
        "2099-09-13T09:00:00+07:00",
        timezone="Asia/Bangkok",
    )

    listing = handle_personal_command(
        store,
        user,
        "!notification",
        target,
        conversation_id=conversation.id,
        is_dm=True,
    )
    assert reminder.id in listing
    assert "กินยา" in listing
    assert handle_personal_command(
        store,
        user,
        "!notification remove กินยา",
        target,
        conversation_id=conversation.id,
        is_dm=True,
    ) == "Reminder removed: กินยา"
    assert (
        handle_personal_command(
            store,
            user,
            "!notification",
            target,
            conversation_id=conversation.id,
            is_dm=True,
        )
        == "No pending reminders."
    )


def test_discord_notification_does_not_remove_duplicate_names(tmp_path, monkeypatch):
    store, user, target = _discord_user(tmp_path)
    monkeypatch.setattr(discord_bot, "active_mode", "agent")
    conversation = store.get_or_create_conversation(user.id, "discord", "10")

    first = store.create_reminder(
        user.id, "กินยา", "2099-09-13T09:00:00+07:00", timezone="Asia/Bangkok"
    )
    second = store.create_reminder(
        user.id, "กินยา", "2099-09-13T12:00:00+07:00", timezone="Asia/Bangkok"
    )

    result = handle_personal_command(
        store, user, "!notification remove กินยา", target,
        conversation_id=conversation.id, is_dm=True,
    )

    assert "Multiple pending reminders" in result
    assert first.id in result
    assert second.id in result
    assert [item.id for item in store.list_reminders(user.id)] == [first.id, second.id]


def test_private_commands_are_rejected_in_guild_without_reading_or_mutating_data(
    tmp_path,
    monkeypatch,
):
    store, user, target = _discord_user(tmp_path)
    monkeypatch.setattr(discord_bot, "active_mode", "agent")
    conversation = store.get_or_create_conversation(user.id, "discord", "100")
    store.add_message(conversation.id, "user", "keep this")
    reminder = store.create_reminder(
        user.id,
        "private appointment",
        "2099-09-13T09:00:00+07:00",
        timezone="Asia/Bangkok",
    )

    for prompt in ("!notification", "!help", "!clear", "!reset all"):
        result = handle_personal_command(
            store,
            user,
            prompt,
            target,
            conversation_id=conversation.id,
            is_dm=False,
        )
        assert result == "This command is private. Please send it to me in Discord DM."

    assert (
        handle_personal_command(
            store,
            user,
            "บ่ายสามวันนี้เข้าประชุม",
            target,
            conversation_id=conversation.id,
            is_dm=False,
        )
        is None
    )

    assert [item.id for item in store.list_reminders(user.id)] == [reminder.id]
    assert store.conversation_history(conversation.id) == [
        {"role": "user", "content": "keep this"}
    ]


def test_discord_dm_help_clear_and_reset_are_user_scoped(tmp_path, monkeypatch):
    store, user, target = _discord_user(tmp_path)
    monkeypatch.setattr(discord_bot, "active_mode", "agent")
    dm = store.get_or_create_conversation(user.id, "discord", "10")
    guild = store.get_or_create_conversation(user.id, "discord", "100")
    other = store.resolve_channel_identity("discord", "20")
    other_dm = store.get_or_create_conversation(other.id, "discord", "20")
    store.add_message(dm.id, "user", "start fresh")
    store.add_message(guild.id, "user", "reset this later")
    store.add_message(other_dm.id, "user", "do not touch")

    help_text = handle_personal_command(
        store,
        user,
        "!help",
        target,
        conversation_id=dm.id,
        is_dm=True,
    )
    assert all(
        command in help_text
        for command in ("!notification", "!clear", "!reset all")
    )
    assert "!noti [" not in help_text
    assert "!daily" not in help_text

    assert "cleared" in handle_personal_command(
        store,
        user,
        "!clear",
        target,
        conversation_id=dm.id,
        is_dm=True,
    )
    assert store.conversation_history(dm.id) == []
    assert store.conversation_history(guild.id) == [
        {"role": "user", "content": "reset this later"}
    ]
    for retired_command in ("/clear", "!brief"):
        assert (
            handle_personal_command(
                store,
                user,
                retired_command,
                target,
                conversation_id=dm.id,
                is_dm=True,
            )
            is None
        )

    result = handle_personal_command(
        store,
        user,
        "!reset all",
        target,
        conversation_id=dm.id,
        is_dm=True,
    )
    assert "deleted (2)" in result
    assert store.conversation_history(guild.id) == []
    assert store.conversation_history(other_dm.id) == [
        {"role": "user", "content": "do not touch"}
    ]


@pytest.mark.asyncio
async def test_discord_delivery_ignores_legacy_daily_briefing_preferences(
    tmp_path, monkeypatch
):
    store, user, _ = _discord_user(tmp_path)
    store.set_user_preference(user.id, "briefing_time", "08:30")
    sent = []

    async def fake_send(delivery, content):
        sent.append((delivery.destination_id, content))

    monkeypatch.setattr(discord_bot, "_send_to_target", fake_send)
    now = "2026-09-13T08:30:00+07:00"

    await deliver_discord_notifications_once(store, now=now)
    await deliver_discord_notifications_once(store, now=now)

    assert sent == []
