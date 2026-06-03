"""
Per-user session memory.

Each authenticated user gets a UserSession that holds:
  - a deque of recent messages (max 30) — word-for-word, backed by the messages table
  - long_term_memory — a summary paragraph, backed by the long_term_memory table
  - working_memory  — current location/activity/mood, backed by the working_memory table

The two-layer logic:
  - Recent messages stay word-for-word in the deque and in the DB.
  - When the deque is full, the OLDEST 15 messages are compressed into long_term_memory
    and then deleted from the DB.  The newest 15 are never summarised.
"""

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass

from google.genai import types

import core.db as db

log = logging.getLogger(__name__)

_MAX     = 30   # deque capacity
_OVERFLOW = 15  # how many old messages to compress when full


# ---------------------------------------------------------------------------
# Working memory
# ---------------------------------------------------------------------------

@dataclass
class WorkingMemory:
    location: str = "apartment"
    activity: str = "unknown"
    mood:     str = "chill"

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


# ---------------------------------------------------------------------------
# Per-user session
# ---------------------------------------------------------------------------

def _text(msg: types.Content) -> str:
    for part in msg.parts:
        if hasattr(part, "text") and part.text:
            return part.text
    return ""


class UserSession:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        # Parallel deques — same index, same message.
        self._contents: deque[types.Content] = deque(maxlen=_MAX)
        self._db_ids:   deque[str | None]    = deque(maxlen=_MAX)
        self.long_term_memory: str           = ""
        self.working_memory: WorkingMemory   = WorkingMemory()
        self.lock = asyncio.Lock()

    # ── Initialisation ───────────────────────────────────────────────────────

    async def load(self) -> None:
        """Populate this session from the database.  Call once after creation."""
        rows, ltm, wm_row = await asyncio.gather(
            db.fetch_messages(self.user_id, limit=_MAX),
            db.fetch_ltm(self.user_id),
            db.fetch_wm(self.user_id),
        )
        for row in rows:
            self._contents.append(
                types.Content(role=row["role"], parts=[types.Part.from_text(text=row["content"])])
            )
            self._db_ids.append(row["id"])

        self.long_term_memory = ltm
        self.working_memory = WorkingMemory(
            location=wm_row.get("location", "apartment"),
            activity=wm_row.get("activity", "unknown"),
            mood=wm_row.get("mood",     "chill"),
        )
        log.debug(
            "Session loaded for %s: %d messages, %d ltm chars.",
            self.user_id, len(self._contents), len(ltm),
        )

    # ── Accessors ────────────────────────────────────────────────────────────

    @property
    def contents(self) -> list[types.Content]:
        return list(self._contents)

    # ── Mutation ─────────────────────────────────────────────────────────────

    async def append(
        self,
        role: str,
        text: str,
        extra_parts: list | None = None,
    ) -> None:
        """Add one message to the session and persist it to the DB.

        extra_parts (e.g. image bytes) are included in the Content sent to
        Gemini but are NOT stored in the DB — only the text is persisted.
        """
        parts = (extra_parts or []) + [types.Part.from_text(text=text)]
        self._contents.append(types.Content(role=role, parts=parts))
        db_id = await db.insert_message(self.user_id, role, text)
        self._db_ids.append(db_id)

    # ── Two-layer memory logic ────────────────────────────────────────────────

    async def compress_rolling(self, gemini_client, model: str) -> None:
        """If the deque is full, compress the oldest _OVERFLOW messages into LTM.

        The newest messages are never summarised — only the overflow is.
        """
        if len(self._contents) < _MAX:
            return

        log.debug("Rolling compression for user %s.", self.user_id)
        old_contents = [self._contents.popleft() for _ in range(_OVERFLOW)]
        old_ids      = [self._db_ids.popleft()   for _ in range(_OVERFLOW)]
        transcript   = "\n".join(_text(m) for m in old_contents)

        try:
            self.long_term_memory = await _compress(
                transcript, self.long_term_memory, gemini_client, model
            )
            await asyncio.gather(
                db.upsert_ltm(self.user_id, self.long_term_memory),
                db.delete_messages([i for i in old_ids if i]),
            )
            log.debug("Rolling compression done for user %s.", self.user_id)
        except Exception as exc:
            log.error("Compression failed for user %s: %s", self.user_id, exc)
            # Restore on failure so we don't lose messages.
            for m, i in zip(reversed(old_contents), reversed(old_ids)):
                self._contents.appendleft(m)
                self._db_ids.appendleft(i)

    async def update_working_memory(self, gemini_client, model: str) -> None:
        """Extract location/activity/mood from recent messages and persist."""
        from core.prompts import WORKING_MEMORY_PROMPT_TEMPLATE
        transcript = "\n".join(_text(m) for m in list(self._contents)[-6:])
        prompt     = WORKING_MEMORY_PROMPT_TEMPLATE.format(transcript=transcript)
        try:
            response = await gemini_client.aio.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            )
            if response.text:
                data = json.loads(response.text.strip())
                self.working_memory.update(
                    data.get("location", "apartment"),
                    data.get("activity", "unknown"),
                    data.get("mood",     "chill"),
                )
                await db.upsert_wm(
                    self.user_id,
                    self.working_memory.location,
                    self.working_memory.activity,
                    self.working_memory.mood,
                )
                log.debug("Working memory updated for user %s: %s", self.user_id, self.working_memory)
        except Exception as exc:
            log.warning("Working memory update failed for user %s (non-critical): %s", self.user_id, exc)

    async def flush(self, gemini_client, model: str) -> None:
        """Compress the entire current session into LTM and clear the message DB rows."""
        if not self._contents:
            return
        transcript = "\n".join(_text(m) for m in self._contents)
        ids        = [i for i in self._db_ids if i]
        try:
            self.long_term_memory = await _compress(
                transcript, self.long_term_memory, gemini_client, model
            )
            await asyncio.gather(
                db.upsert_ltm(self.user_id, self.long_term_memory),
                db.delete_messages(ids),
            )
            self._contents.clear()
            self._db_ids.clear()
            log.debug("Session flushed for user %s.", self.user_id)
        except Exception as exc:
            log.error("Flush failed for user %s: %s", self.user_id, exc)


# ---------------------------------------------------------------------------
# Session pool
# ---------------------------------------------------------------------------

_sessions:   dict[str, UserSession] = {}
_pool_lock = asyncio.Lock()


async def get_session(user_id: str) -> UserSession:
    """Return the live session for a user, creating and loading it if needed."""
    async with _pool_lock:
        if user_id not in _sessions:
            s = UserSession(user_id)
            await s.load()
            _sessions[user_id] = s
        return _sessions[user_id]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _compress(
    transcript: str,
    existing_ltm: str,
    gemini_client,
    model: str,
) -> str:
    from core.prompts import COMPRESSION_PROMPT_TEMPLATE
    prompt = COMPRESSION_PROMPT_TEMPLATE.format(
        long_term_memory=existing_ltm,
        transcript=transcript,
    )
    response = await gemini_client.aio.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
    )
    return response.text.strip() if response.text else existing_ltm
