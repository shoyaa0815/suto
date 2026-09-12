import os
import time

import discord

from ai import ask_local_ai, set_debug_logs
from assistant import AssistantContext
from automation.storage.store import JobStore
from core.language import choose_reply_language
from core.modes import DEFAULT_MODE, get_mode_policy
from tools.file_reader import SUPPORTED_EXTENSIONS

# Discord's own limit on one message. Longer answers are split across several.
MAX_MESSAGE_CHARS = 2000
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
active_mode = DEFAULT_MODE
reply_languages: dict[tuple[int, int], str] = {}
assistant_store: JobStore | None = None


def _get_assistant_store() -> JobStore:
    global assistant_store
    if assistant_store is None:
        assistant_store = JobStore(os.environ.get("SUTO_DB_PATH", "data/suto.db"))
    return assistant_store


@client.event
async def on_ready():
    print(f"bot is online now: {client.user} (mode: {active_mode})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    attachments = [
        a for a in message.attachments
        if a.filename.rsplit(".", 1)[-1].lower() in SUPPORTED_EXTENSIONS
    ]
    if not message.content.strip() and not attachments:
        return

    prompt = message.content
    store = _get_assistant_store()
    user = store.resolve_channel_identity(
        "discord",
        str(message.author.id),
        display_name=message.author.display_name,
        timezone=os.environ.get("SUTO_TIMEZONE", "UTC"),
        locale=os.environ.get("SUTO_LOCALE", "th"),
    )
    conversation = store.get_or_create_conversation(
        user.id,
        "discord",
        str(message.channel.id),
    )
    history = store.conversation_history(conversation.id)
    if message.content.strip():
        store.add_message(conversation.id, "user", message.content)
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

    async with message.channel.typing():
        answer = await ask_local_ai(
            prompt,
            mode=active_mode,
            attachments=attachment_data,
            reply_language=reply_language,
            conversation_history=history,
            assistant_context=(
                AssistantContext(store, user.id, conversation.id)
                if active_mode == "agent"
                else None
            ),
        )

    store.add_message(conversation.id, "assistant", answer)

    for i in range(0, len(answer), MAX_MESSAGE_CHARS):
        await message.channel.send(answer[i:i + MAX_MESSAGE_CHARS])


def run(mode: str):
    global active_mode

    # Validate before opening the Discord connection. main.py already checks
    # this, but keeping the boundary safe also protects direct callers.
    get_mode_policy(mode)
    active_mode = mode
    set_debug_logs(True)

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set (put it in .env)")
    client.run(token)
