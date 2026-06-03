import asyncio
import os
from dotenv import load_dotenv
from core.memory import init_memory
from bot.discord_bot import client as discord_client

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

async def main():
    init_memory()
    await discord_client.start(DISCORD_TOKEN)

asyncio.run(main())