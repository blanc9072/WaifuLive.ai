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


# ---------------------------------------------------------------------------
# calendar permission
# ---------------------------------------------------------------------------

async def fetch_calendar_enabled(user_id: str) -> bool:
    """Return profiles.calendar_enabled (the master switch) for the user.
    Default-deny: no profile row, NULL column, or any DB error all return
    False — never assume consent."""
    def _():
        try:
            rows = (
                _sb().table("profiles")
                .select("calendar_enabled")
                .eq("id", user_id)
                .execute()
                .data
            )
            return bool(rows[0].get("calendar_enabled")) if rows else False
        except Exception:
            log.warning("fetch_calendar_enabled failed for user=%s — defaulting to disabled", user_id)
            return False
    return await _run(_)


async def fetch_calendar_permissions(user_id: str) -> dict:
    """Return the master switch plus per-action calendar grants for the user:
    {"master": bool, "create": bool, "list": bool, "move": bool, "delete": bool}.

    Default-deny across the board: no profile row, a NULL/missing column
    (e.g. pre-migration), or any DB error all resolve every flag to False —
    this never raises, so a caller can trust the dict even before the
    per-action columns exist.
    """
    def _():
        try:
            rows = (
                _sb().table("profiles")
                .select("calendar_enabled,calendar_create_enabled,calendar_list_enabled,"
                        "calendar_move_enabled,calendar_delete_enabled")
                .eq("id", user_id)
                .execute()
                .data
            )
            row = rows[0] if rows else {}
            return {
                "master": bool(row.get("calendar_enabled")),
                "create": bool(row.get("calendar_create_enabled")),
                "list":   bool(row.get("calendar_list_enabled")),
                "move":   bool(row.get("calendar_move_enabled")),
                "delete": bool(row.get("calendar_delete_enabled")),
            }
        except Exception:
            log.warning("fetch_calendar_permissions failed for user=%s — defaulting to all disabled", user_id)
            return {"master": False, "create": False, "list": False, "move": False, "delete": False}
    return await _run(_)



# ---------------------------------------------------------------------------
# scheduling_preferences
#
# THE notes WALL: `notes` (and this table generally) is read ONLY by the
# calendar-blind activity-inference step to phrase a scheduling proposal's
# breakdown, and by the see/reset valve. It must NEVER be selected into,
# concatenated onto, or otherwise routed toward the Stage-0 persona prompt
# (core/prompts.py build_dynamic_prompt) — that function has no import of
# or call into this module, and no caller of the readers below may pass
# their result toward it. If a caller is ever tempted to surface `notes`
# outside the scheduling proposal/breakdown path, generate the copy from
# the numeric fields instead and leave `notes` unused. See the migration
# (supabase/migrations/20260919120000_add_scheduling_preferences.sql) for
# the same wall stated at the schema level.
# ---------------------------------------------------------------------------

_SCHEDULING_PREF_COLUMNS = (
    "activity,default_duration_min,pad_before_min,pad_after_min,tod_pref,notes,updated_at"
)


async def fetch_scheduling_preference(user_id: str, activity: str) -> dict | None:
    """Return the stored scheduling preference row for one normalized
    activity key ("gym", not "Gym" or "the gym"), or None on no row or any
    DB error — never raises. A miss is indistinguishable from "no
    preference learned yet" by design: the inference step falls back to
    its own defaults either way."""
    def _():
        try:
            rows = (
                _sb().table("scheduling_preferences")
                .select(_SCHEDULING_PREF_COLUMNS)
                .eq("user_id", user_id)
                .eq("activity", activity)
                .execute()
                .data
            )
            return rows[0] if rows else None
        except Exception:
            log.warning("fetch_scheduling_preference failed for user=%s activity=%r — treating as no preference",
                        user_id, activity)
            return None
    return await _run(_)


async def list_scheduling_preferences(user_id: str) -> list[dict]:
    """Return every stored scheduling preference for a user, ordered by
    activity — the "what have you learned" valve (Part E). Empty list on
    no rows or any DB error, never raises."""
    def _():
        try:
            return (
                _sb().table("scheduling_preferences")
                .select(_SCHEDULING_PREF_COLUMNS)
                .eq("user_id", user_id)
                .order("activity")
                .execute()
                .data
            )
        except Exception:
            log.warning("list_scheduling_preferences failed for user=%s — defaulting to empty", user_id)
            return []
    return await _run(_)


async def upsert_scheduling_preference(user_id: str, activity: str, fields: dict) -> bool:
    """Write a learned correction. Called ONLY on a confirmed accept (see
    POST /calendar/add_slots) — never speculatively, never before the user
    has actually agreed to the corrected plan. `fields` may include any of
    default_duration_min/pad_before_min/pad_after_min/tod_pref/notes;
    columns not present in `fields` are left at their existing/default
    values via upsert's merge-on-conflict semantics (not overwritten to
    NULL). Returns True on success, False on any DB error — never raises,
    so a failed write degrades to "the correction didn't stick" rather
    than breaking the turn that already added the calendar events."""
    def _():
        try:
            _sb().table("scheduling_preferences").upsert(
                {
                    "user_id": user_id,
                    "activity": activity,
                    **fields,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="user_id,activity",
            ).execute()
            return True
        except Exception:
            log.warning("upsert_scheduling_preference failed for user=%s activity=%r", user_id, activity)
            return False
    return await _run(_)


async def delete_scheduling_preference(user_id: str, activity: str | None = None) -> bool:
    """Reset one learned preference (`activity` given) or ALL of them
    (`activity=None`) — the reset half of the see/reset valve (Part E).
    Returns True on success, False on any DB error — never raises."""
    def _():
        try:
            q = _sb().table("scheduling_preferences").delete().eq("user_id", user_id)
            if activity is not None:
                q = q.eq("activity", activity)
            q.execute()
            return True
        except Exception:
            log.warning("delete_scheduling_preference failed for user=%s activity=%r", user_id, activity)
            return False
    return await _run(_)



async def fetch_username(user_id: str) -> str:
    """Return the email local-part for a user as a username (matches /chat prefix convention)."""
    def _():
        try:
            result = _sb().auth.admin.get_user_by_id(user_id)
            email = (result.user.email or "") if result.user else ""
            return email.split("@")[0] if email else "user"
        except Exception:
            return "user"
    return await _run(_)
