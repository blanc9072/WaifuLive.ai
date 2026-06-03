import asyncio
import logging
import re
import discord
from google.genai import types
from core.gemini import gemini_client, GEMINI_MODEL, generate_reply
from core.memory import (
    chat_session, chat_lock,
    maybe_compress_rolling_memory,
    update_working_memory,
    flush_memory_to_disk,
)
from core.prompts import build_dynamic_prompt

log = logging.getLogger(__name__)

TARGET_CHANNEL_ID = 1311933748438237185
TRIGGER_WORDS     = ["pistachio", "tachi"]
ANDREWS_USERNAME  = "blanc2"



client = discord.Client()


def format_user_message(message: discord.Message) -> str:
    raw = message.content or "[attachment]"
    sanitized = re.sub(r"(?i)(andrew|blanc|blanc\.ai|pistachio)\s*:", r"\1", raw)
    reply_tag = ""
    if message.reference and message.reference.resolved:
        reply_tag = f"[Replying to {message.reference.resolved.author.name}] "
    return f"[{message.author.name}]: {reply_tag}{sanitized}"


def strip_bot_prefix(line: str) -> str:
    lower = line.lower()
    for prefix in ("pistachio:", "tachi:", "[pistachio.ai]:"):
        if lower.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return line


async def send_reply_lines(message: discord.Message, reply: str) -> None:
    lines = [strip_bot_prefix(line) for line in reply.split("\n") if line.strip()]
    for line in lines:
        if len(line) > 1:
            async with message.channel.typing():
                await asyncio.sleep(max(0.8, len(line) * 0.04))
            await message.channel.send(line)
            log.debug("Sent: %s", line)


@client.event
async def on_ready() -> None:
    log.debug("Discord bot online. Channel ID: %s", TARGET_CHANNEL_ID)


@client.event
async def on_message(message: discord.Message) -> None:
    # Ignore self and wrong channel
    if message.author == client.user or message.channel.id != TARGET_CHANNEL_ID:
        return

    # Handle slash commands
    if message.content.startswith("/"):
        if message.content.lower() == "/sleep" and message.author.name == ANDREWS_USERNAME:
            log.debug("Sleep command received — flushing memory and shutting down.")
            await flush_memory_to_disk(gemini_client, GEMINI_MODEL)
            await client.close()
        return

    # Only respond when triggered
    content_lower = message.content.lower()
    is_mentioned = any(word in content_lower for word in TRIGGER_WORDS)
    is_andrew    = message.author.name == ANDREWS_USERNAME

    if not (is_mentioned or is_andrew):
        return

    async with chat_lock:
        log.debug("Triggered by %s", message.author.name)

        chat_session.append(
            types.Content(role="user", parts=[types.Part.from_text(text=format_user_message(message))])
        )

        await maybe_compress_rolling_memory(gemini_client, GEMINI_MODEL)

        try:
            async with message.channel.typing():
                log.debug("Sending to Gemini.")
                reply = await generate_reply(list(chat_session), build_dynamic_prompt())
                log.debug("Raw reply: %r", reply)

            if reply:
                chat_session.append(
                    types.Content(role="model", parts=[types.Part.from_text(text=reply)])
                )
                await send_reply_lines(message, reply)
                asyncio.create_task(update_working_memory(list(chat_session), gemini_client, GEMINI_MODEL))

        except asyncio.TimeoutError:
            log.error("Gemini API timed out.")
            if chat_session:
                chat_session.pop()
        except Exception as exc:
            log.error("Generation error: %s", exc)