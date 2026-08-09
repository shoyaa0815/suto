import os

from dotenv import load_dotenv

from discord_bot import client

load_dotenv()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
