import asyncio
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from ai import ask_local_ai, set_debug_logs
from application.configuration import ProfileSettings, load_settings
from assistant import AssistantContext, DeliveryTargetContext
from workflows.storage.store import JobStore
from application.language import choose_reply_language
from application.modes import DEFAULT_MODE, get_mode_policy
from tools.file_reader import SUPPORTED_EXTENSIONS

# Discord's own limit on one message. Longer answers are split across several.
MAX_MESSAGE_CHARS = 2000
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DISCORD_RETRY_BASE_SECONDS = 5
DISCORD_RETRY_MAX_SECONDS = 60

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
active_mode = DEFAULT_MODE
reply_languages: dict[tuple[int, int], str] = {}
assistant_store: JobStore | None = None
delivery_worker_task: asyncio.Task | None = None
configured_profile: ProfileSettings | None = None


def _get_assistant_store() -> JobStore:
    global assistant_store
    if assistant_store is None:
        assistant_store = JobStore(os.environ.get("SUTO_DB_PATH", "data/suto.db"))
    return assistant_store


def _resolve_discord_user(store: JobStore, message, profile: ProfileSettings):
    return store.resolve_channel_identity(
        "discord",
        str(message.author.id),
        display_name=message.author.display_name,
        timezone=profile.timezone,
        locale=profile.locale,
    )


def should_process_message(message, bot_user) -> bool:
    """Guild messages require a direct bot mention; DMs address the bot directly."""
    if message.author.bot:
        return False
    if message.guild is None:
        return True
    return bot_user is not None and bot_user in message.mentions


def _without_bot_mention(content: str, bot_user_id: int) -> str:
    pattern = rf"<@!?{re.escape(str(bot_user_id))}>"
    return re.sub(pattern, "", content).strip()


def _retry_delay(attempt: int) -> int:
    return min(
        DISCORD_RETRY_BASE_SECONDS * 2 ** (attempt - 1),
        DISCORD_RETRY_MAX_SECONDS,
    )


def _reset_client_for_retry() -> None:
    client.clear()
    # client.run() closes the aiohttp connector. ``clear()`` resets the
    # session but retains that connector, so the next login needs a new one.
    client.http.connector = discord.utils.MISSING


def _discord_delivery_context(message):
    requester_id = str(message.author.id)
    default = DeliveryTargetContext(
        platform="discord",
        destination_id=requester_id,
        destination_type="dm",
        display_name=f"DM with {message.author.display_name}",
        requester_id=requester_id,
    )
    if message.guild is None or message.guild.me is None:
        return default, None, ()

    guild_id = str(message.guild.id)
    available = []
    for channel in message.guild.text_channels:
        bot_permissions = channel.permissions_for(message.guild.me)
        user_permissions = channel.permissions_for(message.author)
        if not (
            bot_permissions.view_channel
            and bot_permissions.send_messages
            and user_permissions.view_channel
            and user_permissions.send_messages
        ):
            continue
        category = f"{channel.category.name} / " if channel.category else ""
        available.append(
            DeliveryTargetContext(
                platform="discord",
                destination_id=str(channel.id),
                destination_type="guild_channel",
                display_name=f"{category}#{channel.name}",
                guild_id=guild_id,
                requester_id=requester_id,
            )
        )
    current = next(
        (
            target
            for target in available
            if target.destination_id == str(message.channel.id)
        ),
        None,
    )
    return default, current, tuple(available)


async def _send_to_target(delivery, content: str) -> None:
    allowed_mentions = discord.AllowedMentions.none()
    if delivery.destination_type == "dm":
        user = client.get_user(int(delivery.destination_id))
        if user is None:
            user = await client.fetch_user(int(delivery.destination_id))
        for index in range(0, len(content), MAX_MESSAGE_CHARS):
            await user.send(
                content[index:index + MAX_MESSAGE_CHARS],
                allowed_mentions=allowed_mentions,
            )
        return

    channel = client.get_channel(int(delivery.destination_id))
    if channel is None:
        channel = await client.fetch_channel(int(delivery.destination_id))
    guild = getattr(channel, "guild", None)
    if guild is None or str(guild.id) != delivery.guild_id:
        raise RuntimeError("delivery channel is not in the recorded server")
    bot_member = guild.me
    if bot_member is None:
        raise RuntimeError("bot membership is unavailable")
    permissions = channel.permissions_for(bot_member)
    if not permissions.view_channel or not permissions.send_messages:
        raise PermissionError("bot cannot view or send to the delivery channel")
    if not delivery.requester_id:
        raise PermissionError("requester identity is unavailable")
    requester = guild.get_member(int(delivery.requester_id))
    if requester is None:
        try:
            requester = await guild.fetch_member(int(delivery.requester_id))
        except discord.NotFound as error:
            raise PermissionError("requester is no longer in the server") from error
    requester_permissions = channel.permissions_for(requester)
    if not (
        requester_permissions.view_channel
        and requester_permissions.send_messages
    ):
        raise PermissionError(
            "requester can no longer view or send to the delivery channel"
        )
    for index in range(0, len(content), MAX_MESSAGE_CHARS):
        await channel.send(
            content[index:index + MAX_MESSAGE_CHARS],
            allowed_mentions=allowed_mentions,
        )


async def _send_reminder(delivery) -> None:
    await _send_to_target(delivery, delivery.title)


def _reminder_time(reminder) -> str:
    instant = datetime.fromisoformat(reminder.remind_at)
    try:
        instant = instant.astimezone(ZoneInfo(reminder.timezone))
    except ZoneInfoNotFoundError:
        pass
    return instant.strftime("%Y-%m-%d %H:%M %Z")


def _discord_help() -> str:
    lines = [
        "Discord DM commands:",
        "!help — show available commands",
        "!clear — clear this DM chat context",
        "!reset all — delete all of your saved chat contexts",
    ]
    if active_mode == "agent":
        lines.extend(
            (
                "!notification [remove <name>] — list or remove reminders by name",
            )
        )
    return "\n".join(lines)


def handle_personal_command(
    store,
    user,
    prompt: str,
    default_target,
    *,
    conversation_id: str | None = None,
    is_dm: bool = True,
) -> str | None:
    """Handle Discord text equivalents of the CLI's personal commands."""
    parts = prompt.split()
    if not parts:
        return None

    command = parts[0].casefold()
    if command not in {"!notification", "!help", "!clear", "!reset"}:
        return None

    if not is_dm:
        return "This command is private. Please send it to me in Discord DM."

    if command == "!help":
        return _discord_help() if len(parts) == 1 else "usage: !help"

    if command == "!clear":
        if len(parts) != 1:
            return "usage: !clear"
        if conversation_id is None:
            raise ValueError("conversation context is unavailable")
        store.clear_conversation(conversation_id)
        return "This DM chat context has been cleared."

    if command == "!reset":
        if len(parts) != 2 or parts[1].casefold() != "all":
            return "usage: !reset all"
        count = store.reset_user_conversations(user.id)
        return f"All your saved chat contexts have been deleted ({count})."

    if active_mode != "agent":
        return "Personal commands are available only in agent mode."

    if command == "!notification":
        if len(parts) == 1:
            reminders = store.list_reminders(user.id, limit=None)
            if not reminders:
                return "No pending reminders."
            lines = ["Pending reminders:"]
            lines.extend(
                f"{item.id}  {_reminder_time(item)}  {item.title}"
                for item in reminders
            )
            return "\n".join(lines)
        if len(parts) >= 3 and parts[1].casefold() == "remove":
            title = " ".join(parts[2:])
            reminder, matches = store.cancel_reminder_by_title(user.id, title)
            if len(matches) > 1:
                lines = [f'Multiple pending reminders are named "{title}":']
                lines.extend(
                    f"{item.id}  {_reminder_time(item)}  {item.title}"
                    for item in matches
                )
                return "\n".join(lines)
            if reminder is None:
                return f"Reminder not found: {title}"
            return f"Reminder removed: {reminder.title}"
        return "usage: !notification [remove <name>]"


async def deliver_discord_notifications_once(store, *, now: str | None = None) -> None:
    deliveries = store.claim_due_reminder_deliveries("discord", now=now)
    for delivery in deliveries:
        if not store.reminder_delivery_is_current(delivery):
            continue
        try:
            await _send_reminder(delivery)
        except Exception as error:
            status = store.fail_reminder_delivery(
                delivery.reminder_id,
                f"{type(error).__name__}: {error}",
            )
            print(
                f"[discord reminder] id={delivery.reminder_id} "
                f"status={status} error={type(error).__name__}: {error}"
            )
        else:
            store.complete_reminder_delivery(delivery.reminder_id)

async def deliver_discord_reminders() -> None:
    """Deliver persistent Discord reminders."""
    store = _get_assistant_store()
    while not client.is_closed():
        try:
            await deliver_discord_notifications_once(store)
        except Exception as error:
            print(f"[discord delivery worker] {type(error).__name__}: {error}")
        await asyncio.sleep(3)


@client.event
async def on_ready():
    global delivery_worker_task

    print(f"bot is online now: {client.user} (mode: {active_mode})")
    if delivery_worker_task is None or delivery_worker_task.done():
        delivery_worker_task = asyncio.create_task(deliver_discord_reminders())


@client.event
async def on_message(message: discord.Message):
    if not should_process_message(message, client.user):
        return

    attachments = [
        a for a in message.attachments
        if a.filename.rsplit(".", 1)[-1].lower() in SUPPORTED_EXTENSIONS
    ]
    prompt = _without_bot_mention(message.content, client.user.id)
    if not prompt and not attachments:
        return

    store = _get_assistant_store()
    profile = configured_profile or load_settings().profile
    user = _resolve_discord_user(store, message, profile)
    conversation = store.get_or_create_conversation(
        user.id,
        "discord",
        str(message.channel.id),
    )
    history = store.conversation_history(conversation.id)
    language_key = (message.channel.id, message.author.id)
    reply_language = choose_reply_language(
        message.content,
        previous_code=reply_languages.get(language_key),
    )
    reply_languages[language_key] = reply_language.code
    attachment_data = {}
    attachment_lines = []
    download_started = time.perf_counter()
    for index, attachment in enumerate(attachments, start=1):
        attachment_id = str(index)
        if attachment.size > MAX_ATTACHMENT_BYTES:
            attachment_lines.append(
                f"- attachment_id={attachment_id}, filename={attachment.filename}, "
                "status=too_large"
            )
            continue
        data = await attachment.read()
        attachment_data[attachment_id] = (attachment.filename, data)
        attachment_lines.append(
            f"- attachment_id={attachment_id}, filename={attachment.filename}"
        )

    if attachments:
        elapsed_ms = round((time.perf_counter() - download_started) * 1000)
        print(
            f"[timing] event=attachment_download ms={elapsed_ms} "
            f"files={len(attachment_data)} "
            f"bytes={sum(len(data) for _, data in attachment_data.values())}"
        )

    if attachment_lines:
        prompt += "\n\nAttached files available:\n" + "\n".join(attachment_lines)

    default_target, current_target, available_targets = _discord_delivery_context(
        message
    )

    try:
        command_answer = handle_personal_command(
            store,
            user,
            prompt,
            default_target,
            conversation_id=conversation.id,
            is_dm=message.guild is None,
        )
    except ValueError as error:
        command_answer = str(error)
    if command_answer is not None:
        normalized_command = [part.casefold() for part in prompt.split()]
        if message.guild is None and normalized_command in (
            ["!clear"],
            ["!reset", "all"],
        ):
            reply_languages.pop(language_key, None)
        for i in range(0, len(command_answer), MAX_MESSAGE_CHARS):
            await message.channel.send(
                command_answer[i:i + MAX_MESSAGE_CHARS],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return

    if prompt:
        store.add_message(conversation.id, "user", prompt)
    async with message.channel.typing():
        answer = await ask_local_ai(
            prompt,
            mode=active_mode,
            attachments=attachment_data,
            reply_language=reply_language,
            conversation_history=history,
            assistant_context=(
                AssistantContext(
                    store,
                    user.id,
                    conversation.id,
                    default_delivery_target=default_target,
                    current_delivery_target=current_target,
                    available_delivery_targets=available_targets,
                )
                if active_mode == "agent"
                else None
            ),
        )

    store.add_message(conversation.id, "assistant", answer)

    for i in range(0, len(answer), MAX_MESSAGE_CHARS):
        await message.channel.send(
            answer[i:i + MAX_MESSAGE_CHARS],
            allowed_mentions=discord.AllowedMentions.none(),
        )


def run(mode: str):
    global active_mode, configured_profile

    # Validate before opening the Discord connection. main.py already checks
    # this, but keeping the boundary safe also protects direct callers.
    if mode == "home":
        raise ValueError("home mode is available only in the plain terminal")
    get_mode_policy(mode)
    active_mode = mode
    configured_profile = load_settings().profile
    set_debug_logs(True)

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set (put it in .env)")
    attempts = 0
    while True:
        try:
            client.run(token)
            return
        except discord.DiscordServerError as error:
            reason = f"Discord HTTP {error.status} during login"
        except AttributeError as error:
            # discord.py 2.7.1 can try to resume an initial failed gateway
            # connection and dereference ``self.ws`` before it exists. The
            # preceding gateway error (such as HTTP 503) is logged by the
            # library; restart from a clean client state instead.
            if "'NoneType' object has no attribute 'sequence'" not in str(error):
                raise
            reason = "initial gateway connection failed"

        attempts += 1
        delay = _retry_delay(attempts)
        print(f"[discord] {reason}; retrying in {delay}s")
        _reset_client_for_retry()
        time.sleep(delay)
