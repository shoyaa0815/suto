import discord
from discord import app_commands

from ai import ask_local_ai
from db import clear_history, init_db
from file_reader import SUPPORTED_EXTENSIONS, extract_text

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

init_db()


@client.event
async def on_ready():
    await tree.sync()
    print(f"bot is online now: {client.user}")


@tree.command(name="clear", description="Clear this channel's conversation memory with the AI")
async def clear(interaction: discord.Interaction):
    clear_history(str(interaction.channel_id))
    await interaction.response.send_message("conversation memory cleared", ephemeral=True)


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
        answer = await ask_local_ai(str(message.channel.id), prompt)

    for i in range(0, len(answer), 2000):
        await message.channel.send(answer[i:i + 2000])
