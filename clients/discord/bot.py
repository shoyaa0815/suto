import os

import discord
from discord import app_commands

from ai import ask_local_ai
from file_reader import SUPPORTED_EXTENSIONS, extract_text

# Discord's own limit on one message. Longer answers are split across several.
MAX_MESSAGE_CHARS = 2000

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    await tree.sync()
    print(f"bot is online now: {client.user}")


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
    for attachment in attachments:
        data = await attachment.read()
        text = extract_text(attachment.filename, data)
        prompt += f"\n\n[attached file: {attachment.filename}]\n{text}"

    async with message.channel.typing():
        answer = await ask_local_ai(prompt)

    for i in range(0, len(answer), MAX_MESSAGE_CHARS):
        await message.channel.send(answer[i:i + MAX_MESSAGE_CHARS])


def run():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set (put it in .env)")
    client.run(token)
