"""
Supabase access layer for the backend.

Uses the service role key so RLS is bypassed server-side — this is the
correct pattern for trusted backend code.  The anon key is for clients only.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from supabase import create_client, Client

log = logging.getLogger(__name__)

_client: Client | None = None


def _sb() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        if not key:
            raise RuntimeError(
                "SUPABASE_SERVICE_KEY is not set.  "
                "Add it to .env — Supabase Dashboard → Project Settings → API → service_role."
            )
        _client = create_client(url, key)
    return _client


async def _run(fn):
    return await asyncio.to_thread(fn)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

async def fetch_messages(user_id: str, limit: int = 30) -> list[dict]:
    """Return the most recent `limit` messages for a user, oldest first."""
    def _():
        return (
            _sb().table("messages")
            .select("id,role,content")
            .eq("user_id", user_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
            .data
        )
    return await _run(_)


async def insert_message(user_id: str, role: str, content: str) -> str:
    """Insert a message row and return its id."""
    def _():
        return (
            _sb().table("messages")
            .insert({"user_id": user_id, "role": role, "content": content})
            .execute()
            .data[0]["id"]
        )
    return await _run(_)


async def delete_messages(ids: list[str]) -> None:
    if not ids:
        return
    def _():
        _sb().table("messages").delete().in_("id", ids).execute()
    await _run(_)


# ---------------------------------------------------------------------------
# long_term_memory
# ---------------------------------------------------------------------------

async def fetch_ltm(user_id: str) -> str:
    def _():
        rows = (
            _sb().table("long_term_memory")
            .select("summary")
            .eq("user_id", user_id)
            .execute()
            .data
        )
        return rows[0]["summary"] if rows else ""
    return await _run(_)


async def upsert_ltm(user_id: str, summary: str) -> None:
    def _():
        _sb().table("long_term_memory").upsert(
            {"user_id": user_id, "summary": summary,
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="user_id",
        ).execute()
    await _run(_)


# ---------------------------------------------------------------------------
# working_memory
# ---------------------------------------------------------------------------

async def fetch_wm(user_id: str) -> dict:
    def _():
        rows = (
            _sb().table("working_memory")
            .select("location,activity,mood")
            .eq("user_id", user_id)
            .execute()
            .data
        )
        return rows[0] if rows else {"location": "apartment", "activity": "unknown", "mood": "chill"}
    return await _run(_)


async def upsert_wm(user_id: str, location: str, activity: str, mood: str) -> None:
    def _():
        _sb().table("working_memory").upsert(
            {"user_id": user_id, "location": location, "activity": activity,
             "mood": mood, "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="user_id",
        ).execute()
    await _run(_)


# ---------------------------------------------------------------------------
# avatar
# ---------------------------------------------------------------------------

async def fetch_avatar_config(user_id: str) -> dict:
    """Return {file_path, expression_map} for the user's avatar; falls back to the is_default avatar."""
    def _():
        profile_rows = (
            _sb().table("profiles")
            .select("avatar_id")
            .eq("id", user_id)
            .execute()
            .data
        )
        avatar_id = profile_rows[0].get("avatar_id") if profile_rows else None

        if avatar_id:
            rows = (
                _sb().table("avatars")
                .select("file_path,expression_map")
                .eq("id", avatar_id)
                .execute()
                .data
            )
            if rows and rows[0].get("file_path"):
                return {"file_path": rows[0]["file_path"], "expression_map": rows[0].get("expression_map")}

        # No profile row, avatar_id is NULL, or avatar missing file_path → use default.
        rows = (
            _sb().table("avatars")
            .select("file_path,expression_map")
            .eq("is_default", True)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            return {"file_path": rows[0]["file_path"], "expression_map": rows[0].get("expression_map")}
        return {"file_path": None, "expression_map": None}
    return await _run(_)


# ---------------------------------------------------------------------------
# voice
# ---------------------------------------------------------------------------

async def fetch_voice_tts_ref(user_id: str) -> str | None:
    """Return tts_ref for the user's voice; falls back to the is_default voice."""
    def _():
        profile_rows = (
            _sb().table("profiles")
            .select("voice_id")
            .eq("id", user_id)
            .execute()
            .data
        )
        voice_id = profile_rows[0].get("voice_id") if profile_rows else None

        if voice_id:
            rows = (
                _sb().table("voices")
                .select("tts_ref")
                .eq("id", voice_id)
                .execute()
                .data
            )
            if rows and rows[0].get("tts_ref"):
                return rows[0]["tts_ref"]

        # No profile row, voice_id is NULL, or voice missing tts_ref → use default.
        rows = (
            _sb().table("voices")
            .select("tts_ref")
            .eq("is_default", True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0]["tts_ref"] if rows else None
    return await _run(_)
