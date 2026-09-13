from types import SimpleNamespace

from clients.discord.bot import (
    _discord_delivery_context,
    _without_bot_mention,
    should_process_message,
)


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
