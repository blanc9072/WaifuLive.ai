from collections import deque
from dataclasses import dataclass
import os
import asyncio
import logging
import json
from google.genai import types

log = logging.getLogger(__name__)

MEMORY_FILE = "tachi_memory.json"


def _content_text(msg) -> str:
    """Return the text portion of a Content, skipping image/blob parts."""
    for part in msg.parts:
        if hasattr(part, 'text') and part.text:
            return part.text
    return ''
TARGET_CHANNEL_ID = 1311933748438237185

# ---------------------------------------------------------------------------
# Working memory (session-only, resets on restart)
# ---------------------------------------------------------------------------

@dataclass
class WorkingMemory:
    location: str = "apartment"
    activity: str = "unknown"
    mood: str = "chill"

    def to_prompt_block(self) -> str:
        return (
            "[Right Now — ground yourself in this before responding]\n"
            f"  where    : {self.location}\n"
            f"  what     : {self.activity}\n"
            f"  vibe     : {self.mood}"
        )

    def update(self, location: str, activity: str, mood: str) -> None:
        if location and location.lower() != "apartment": self.location = location
        if activity and activity.lower() != "unknown":   self.activity = activity
        if mood     and mood.lower()     != "chill":     self.mood     = mood


working_memory = WorkingMemory()

# ---------------------------------------------------------------------------
# Chat session (short-term memory)
# ---------------------------------------------------------------------------

chat_session: deque[types.Content] = deque(maxlen=30)
chat_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Long-term memory
# ---------------------------------------------------------------------------

long_term_memory: str = ""


def load_memory(channel_id: int) -> str:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f).get(str(channel_id), "")
    return ""


def save_memory(channel_id: int, summary: str) -> None:
    data: dict = {}
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            data = json.load(f)
    data[str(channel_id)] = summary
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=4)


def init_memory() -> None:
    """Call once at startup to load long-term memory from disk."""
    global long_term_memory
    long_term_memory = load_memory(TARGET_CHANNEL_ID)
    log.debug("Long-term memory loaded (%d chars).", len(long_term_memory))


async def compress_memory(transcript: str, gemini_client, model: str) -> str:
    """Compress a transcript into the long-term memory paragraph."""
    from core.prompts import COMPRESSION_PROMPT_TEMPLATE
    prompt = COMPRESSION_PROMPT_TEMPLATE.format(
        long_term_memory=long_term_memory,
        transcript=transcript,
    )
    response = await gemini_client.aio.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
    )
    return response.text.strip() if response.text else long_term_memory


async def update_working_memory(recent_messages: list[types.Content], gemini_client, model: str) -> None:
    """Extract location/activity/mood from recent messages and update working_memory."""
    from core.prompts import WORKING_MEMORY_PROMPT_TEMPLATE
    transcript = "\n".join(_content_text(msg) for msg in recent_messages[-6:])
    prompt = WORKING_MEMORY_PROMPT_TEMPLATE.format(transcript=transcript)
    try:
        response = await gemini_client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        )
        if response.text:
            data = json.loads(response.text.strip())
            working_memory.update(
                location=data.get("location", "apartment"),
                activity=data.get("activity", "unknown"),
                mood=data.get("mood", "chill"),
            )
            log.debug("Working memory updated: %s", working_memory)
    except Exception as exc:
        log.warning("Working memory update failed (non-critical): %s", exc)


async def maybe_compress_rolling_memory(gemini_client, model: str) -> None:
    """If chat_session is full, compress and evict the oldest 15 messages."""
    global long_term_memory

    if len(chat_session) < 30:
        return

    log.debug("Memory full — compressing oldest 15 messages.")
    old_messages = [chat_session.popleft() for _ in range(15)]
    transcript = "\n".join(_content_text(msg) for msg in old_messages)

    try:
        long_term_memory = await compress_memory(transcript, gemini_client, model)
        await asyncio.to_thread(save_memory, TARGET_CHANNEL_ID, long_term_memory)
        log.debug("Rolling memory compressed and saved.")
    except Exception as exc:
        log.error("Memory compression failed: %s", exc)
        for msg in reversed(old_messages):
            chat_session.appendleft(msg)


async def flush_memory_to_disk(gemini_client, model: str) -> None:
    """Compress entire current session into long-term memory and save to disk."""
    global long_term_memory

    if not chat_session:
        return

    transcript = "\n".join(_content_text(msg) for msg in chat_session)
    try:
        long_term_memory = await compress_memory(transcript, gemini_client, model)
        await asyncio.to_thread(save_memory, TARGET_CHANNEL_ID, long_term_memory)
        log.debug("Memory flushed to disk.")
    except Exception as exc:
        log.error("Memory flush failed: %s", exc)