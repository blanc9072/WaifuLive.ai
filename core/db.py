"""
Supabase access layer for the backend.

Uses the service role key so RLS is bypassed server-side — this is the
correct pattern for trusted backend code.  The anon key is for clients only.
"""

import asyncio
import logging
import os

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
            {"user_id": user_id, "summary": summary}
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
            {"user_id": user_id, "location": location, "activity": activity, "mood": mood}
        ).execute()
    await _run(_)
