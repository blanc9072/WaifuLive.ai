import asyncio
import logging
import os
import uvicorn
from dotenv import load_dotenv
from core.memory import init_memory
from bot.discord_bot import client as discord_client
from api.routes import app

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))


async def run_api():
    config = uvicorn.Config(app, host=API_HOST, port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


async def run_discord():
    await discord_client.start(DISCORD_TOKEN)


async def main():
    init_memory()
    log.debug("Starting Pistachio API on %s:%s", API_HOST, API_PORT)
    await run_api()


if __name__ == "__main__":
    asyncio.run(main())