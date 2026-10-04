import asyncio
import logging
import base64
import os
import re
import tempfile
from datetime import datetime, timedelta
from faster_whisper import WhisperModel
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from supabase import create_client, Client as SupabaseClient
from google.genai import types
from core.gemini import gemini_client, GEMINI_MODEL, generate_reply, SAFETY_SETTINGS
from core.memory import get_session
from core.prompts import build_dynamic_prompt
from core.tool_resolver import resolve_calendar_action
from core.calendar_executor import (
    create_calendar_event, delete_event, delete_by_uid, move_event, move_by_uid,
    _resolve_list_date, _parse_dt,
)
from core.scheduler import infer_block, place_slots
import core.calendar_read_ek as ek_read
import core.db as db
import core.tts as tts

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase auth (anon key — only used to verify JWTs, not for DB access)
# ---------------------------------------------------------------------------

_supabase: SupabaseClient | None = None
_bearer = HTTPBearer()


def _get_supabase() -> SupabaseClient:
    global _supabase
    if _supabase is None:
        # Use service key — anon key can misbehave with auth.get_user() on some
        # supabase-py versions when called server-side without an active session.
        _supabase = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    return _supabase


async def verify_supabase_token(token: str) -> str | None:
    """Verify a Supabase JWT; return the user_id or None on any failure."""
    try:
        result = await asyncio.to_thread(_get_supabase().auth.get_user, token)
        return result.user.id
    except Exception:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """Verify the Supabase JWT and return the user_id (UUID string).

    user_id comes exclusively from the verified token — never from the
    request body — so a client cannot impersonate another user.
    """
    user_id = await verify_supabase_token(credentials.credentials)
    if user_id:
        return user_id
    try:
        await asyncio.to_thread(_get_supabase().auth.get_user, credentials.credentials)
    except Exception as e:
        raw = str(e)
        # Classify the failure reason explicitly for diagnostics.
        if "expired" in raw.lower():
            reason = "EXPIRED_TOKEN"
        elif "invalid" in raw.lower() and "signature" in raw.lower():
            reason = "SIGNATURE_MISMATCH"
        elif "malformed" in raw.lower() or "invalid jwt" in raw.lower():
            reason = "MALFORMED_TOKEN"
        elif "audience" in raw.lower():
            reason = "WRONG_AUDIENCE"
        elif "not found" in raw.lower() or "user" in raw.lower():
            reason = "USER_NOT_FOUND"
        else:
            reason = "UNKNOWN"
        log.warning(
            "Token verification FAILED reason=%s exc_type=%s exc=%s token_prefix=%s",
            reason,
            type(e).__name__,
            raw,
            credentials.credentials[:40] if credentials.credentials else "none",
        )
        raise HTTPException(status_code=401, detail=f"Invalid or expired token. [{reason}]")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

VISION_MODEL = "gemini-2.5-flash"
MEMORY_MODEL = "gemini-2.5-flash"

# Process-wide flag: flipped to False on first multimodal failure so we
# silently fall back to the description path without ever retrying that way.
_direct_vision_ok = True

_NUDGE_MAX_PER_DAY = 6
_NUDGE_MIN_GAP_SEC = 45 * 60
_nudge_log: dict[str, list[float]] = {}

# Resolver tool name -> per-action permission key in fetch_calendar_permissions().
# create_event/list_events/delete_event/move_event/suggest_slots are all
# resolved today. suggest_slots maps to "list" — proposing a plan only
# READS the calendar; nothing is written until POST /calendar/add_slots,
# which is separately gated on "create". Any tool not in this map is
# treated as unmapped and denied by default.
_CALENDAR_TOOL_PERMISSIONS = {
    "create_event":  "create",
    "list_events":   "list",
    "move_event":    "move",
    "delete_event":  "delete",
    "suggest_slots": "list",
}


def _nudge_allowed(user_id: str) -> bool:
    import time
    now = time.time()
    hits = [t for t in _nudge_log.get(user_id, []) if now - t < 86400]
    _nudge_log[user_id] = hits
    if len(hits) >= _NUDGE_MAX_PER_DAY: return False
    if hits and now - hits[-1] < _NUDGE_MIN_GAP_SEC: return False
    return True


def _nudge_record(user_id: str) -> None:
    import time
    _nudge_log.setdefault(user_id, []).append(time.time())

app = FastAPI(title="Pistachio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Whisper (on-device STT — base.en int8, ~154 MB, downloaded on first use)
# ---------------------------------------------------------------------------

_whisper: WhisperModel | None = None


def _get_whisper() -> WhisperModel:
    global _whisper
    if _whisper is None:
        # Downloads to ~/.cache/huggingface/hub/ on first call (~154 MB).
        # int8 quantization: CPU-friendly, fast enough for dictation bursts.
        _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        log.info("Whisper base.en loaded.")
    return _whisper


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    username:   str
    message:    str
    screenshot: str | None = None


class ChatResponse(BaseModel):
    reply:           str
    working_memory:  dict
    audio_b64:       str | None = None
    calendar_action: dict | None = None


class DeleteByUidRequest(BaseModel):
    uid:      str
    calendar: str


class MoveByUidRequest(BaseModel):
    uid:       str
    calendar:  str
    new_start: str
    new_end:   str | None = None


class SlotIn(BaseModel):
    start: str
    end:   str


class BreakdownItemIn(BaseModel):
    label:   str
    minutes: int


class PrefDeltaIn(BaseModel):
    """The preference to persist alongside an accepted plan (S4). On
    POST /calendar/add_slots this should carry the FULL current
    pad_before_min/pad_after_min/default_duration_min/tod_pref the
    accepted plan actually used — NOT a sparse "only what changed"
    delta. Two of those columns are `not null default 0` in the schema
    (see supabase/migrations/20260919120000_add_scheduling_preferences.sql):
    writing only the touched field on a first-ever insert would let
    Postgres silently fill the OTHER one with its 0 default, which then
    reads back indistinguishable from a real correction the user never
    made — always sending the full resolved state avoids that.
    (/calendar/correct_plan's own OWN response `pref_delta` field is a
    genuinely sparse per-turn delta for the client to track what THIS
    correction changed — a different, narrower use of the same shape.)
    `echo` is the confirmation phrasing shown to the user; the only place
    it may land server-side is the scheduling_preferences.notes column
    (display copy for a later "what have you learned" valve) — see THE
    notes WALL, it never reaches the persona prompt."""
    pad_before_min:       int | None = None
    pad_after_min:        int | None = None
    default_duration_min: int | None = None
    tod_pref:             str | None = None
    echo:                 str | None = None


class AddSlotsRequest(BaseModel):
    activity:   str
    slots:      list[SlotIn]
    pref_delta: PrefDeltaIn | None = None


class CorrectPlanRequest(BaseModel):
    activity:             str
    correction_text:      str
    pad_before_min:       int
    pad_after_min:        int
    default_duration_min: int
    tod_pref:             str | None = None
    breakdown:            list[BreakdownItemIn]
    window_start:         str
    window_end:           str
    count:                int
    day_exclusions:       list[str] | None = None


class TranscribeRequest(BaseModel):
    audio_b64: str   # base64-encoded WAV at any sample rate


class NudgeRequest(BaseModel):
    username:   str
    context:    str | None = None
    screenshot: str | None = None


class NudgeResponse(BaseModel):
    message:        str
    working_memory: dict
    audio_b64:      str | None = None


class MemoryResponse(BaseModel):
    long_term: str
    working:   dict


# ---------------------------------------------------------------------------
# Vision helpers
# ---------------------------------------------------------------------------

async def describe_screen(image_bytes: bytes) -> str:
    """Fallback: use a standard vision model to describe the screenshot as text."""
    try:
        response = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=VISION_MODEL,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                    types.Part.from_text(
                        text="Describe what is visible on this screen in 1-2 sentences. "
                             "Be specific about apps, windows, and content shown."
                    ),
                ])],
                config=types.GenerateContentConfig(max_output_tokens=150, temperature=0.1),
            ),
            timeout=15.0,
        )
        return response.text.strip() if response.text else ""
    except Exception as exc:
        log.warning("Screen description failed: %s", exc)
        return ""


def _build_parts(message_text: str, image_bytes: bytes | None, use_direct: bool) -> list:
    text_part = types.Part.from_text(text=message_text)
    if image_bytes and use_direct:
        return [types.Part.from_bytes(data=image_bytes, mime_type="image/png"), text_part]
    return [text_part]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe_audio(
    req: TranscribeRequest,
    user_id: str = Depends(get_current_user),
):
    """Transcribe a base64-encoded WAV using on-device Whisper (base.en int8).
    First call downloads the model (~154 MB); subsequent calls are fast.
    Transcription is on-stop, not streaming — returns the full transcript."""
    audio_bytes = base64.b64decode(req.audio_b64)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        def _run() -> str:
            model = _get_whisper()
            # faster-whisper returns a generator; consume it fully inside the thread.
            segments, _ = model.transcribe(tmp_path, beam_size=5, language="en")
            return " ".join(s.text.strip() for s in segments).strip()

        transcript = await asyncio.to_thread(_run)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    log.debug("Transcribed %d bytes → %r", len(audio_bytes), transcript[:60])
    return {"transcript": transcript}


# ---------------------------------------------------------------------------
# Calendar reads — EK1. Reads that used to hit a live, contention-prone
# multi-calendar osascript scan (core.calendar_executor.list_events/
# list_events_window), then briefly a Supabase snapshot cache (CS1-CS3, torn
# down in EK2 — see git history for that cache's design if it's ever needed
# again), now read live via EventKit (core.calendar_read_ek).
#
# Why: osascript's `every event whose start date >= X and start date <= Y`
# tests a RECURRING event's MASTER start date and never expands individual
# occurrences — a benchmark class schedule (9 weekly recurring occurrences
# on one day) came back 0/9 through that path, cache or no cache. EventKit's
# predicateForEventsWithStartDate_endDate_calendars_ expands recurrence
# correctly (confirmed 9/9 in the probe spike) and answers a full
# multi-calendar window in single-digit milliseconds — fast enough that no
# cache is needed at all; a write is visible on the very next read.
#
# Deliberately lives HERE (api/routes.py), not inside core/calendar_
# executor.py: that module explicitly does NOT import Supabase/db (see
# its module docstring — "so the voice relay can reuse these functions
# unchanged"), and while calendar_read_ek.py itself also avoids that
# import, keeping these read-consumer functions alongside the scheduler
# orchestration immediately below (which already composes calendar_
# executor + core.db + the model) keeps every "seam" function in one
# place.
#
# WRITES (create/move/delete) are explicitly UNCHANGED — they still go
# straight through calendar_executor's live osascript functions. Only
# DISPLAY reads and the scheduler's PROPOSE step (never its pre-create
# re-verify — see calendar_add_slots, which re-verifies via its OWN
# separate EventKit read, fresh on that later turn) are redirected here.
# ---------------------------------------------------------------------------

_EK_PERMISSION_MESSAGE = (
    "I need Calendar access — grant Full Calendar access in "
    "System Settings › Privacy & Security › Calendars."
)


async def _list_events_from_ek(args: dict, enabled: bool) -> dict:
    """EK1: the DISPLAY read (list_events' single-day AND range paths),
    now via EventKit instead of osascript. Mirrors calendar_executor.
    list_events's exact single-day-vs-range argument contract (reusing
    its OWN pure parsing helpers, _resolve_list_date/_parse_dt), so the
    resolver's args work identically regardless of which read backend
    serves them. writable_only=False — read-only calendars (a subscribed
    holidays calendar, Birthdays) are real and visible here, same
    invariant list_events() itself documents.

    Never raises. A permission miss (EventKit access not fullAccess) is
    surfaced as status="needs_permission" with a distinct message, never
    a bare empty "success" — see calendar_read_ek.read_window's docstring
    for why that distinction is load-bearing.
    """
    window_start_str = (args.get("window_start") or "").strip()
    window_end_str = (args.get("window_end") or "").strip()

    if window_start_str or window_end_str:
        start_dt = _parse_dt(window_start_str) if window_start_str else None
        end_dt = _parse_dt(window_end_str) if window_end_str else None
        if start_dt is None or end_dt is None or end_dt < start_dt:
            return {"status": "error", "kind": "list", "events": [],
                    "message": "Couldn't tell what date range to check."}
        window_start_iso = start_dt.strftime("%Y-%m-%d")
        window_end_iso = end_dt.strftime("%Y-%m-%d")

        if not enabled:
            return {"status": "app_disabled", "kind": "list", "events": [],
                    "window_start": window_start_iso, "window_end": window_end_iso,
                    "message": "Calendar actions are turned off."}

        range_start = start_dt
        range_end = end_dt.replace(hour=23, minute=59, second=59)
        read = await ek_read.read_window(range_start, range_end, writable_only=False)
        if read["status"] == "needs_permission":
            return {"status": "needs_permission", "kind": "list", "events": [],
                    "window_start": window_start_iso, "window_end": window_end_iso,
                    "message": _EK_PERMISSION_MESSAGE}
        if read["status"] != "ok":
            return {"status": "error", "kind": "list", "events": [],
                    "window_start": window_start_iso, "window_end": window_end_iso,
                    "message": "Couldn't read the calendar."}
        return {"status": "success", "kind": "list",
                "window_start": window_start_iso, "window_end": window_end_iso,
                "events": read["events"]}

    target = _resolve_list_date(args.get("date"))
    date_iso = target.strftime("%Y-%m-%d")

    if not enabled:
        return {"status": "app_disabled", "kind": "list", "events": [],
                "date": date_iso, "message": "Calendar actions are turned off."}

    start_dt = target
    end_dt = target.replace(hour=23, minute=59, second=59)
    read = await ek_read.read_window(start_dt, end_dt, writable_only=False)
    if read["status"] == "needs_permission":
        return {"status": "needs_permission", "kind": "list", "events": [],
                "date": date_iso, "message": _EK_PERMISSION_MESSAGE}
    if read["status"] != "ok":
        return {"status": "error", "kind": "list", "events": [],
                "date": date_iso, "message": "Couldn't read the calendar."}
    return {"status": "success", "kind": "list", "date": date_iso, "events": read["events"]}


async def _ek_busy_events(
    start_dt: datetime, end_dt: datetime, writable_only: bool,
) -> tuple[str, list[dict]]:
    """The scheduler's PROPOSE-step busy-set, read live via EventKit (EK1)
    instead of a live osascript list_events_window() scan or the (now
    orphaned) snapshot cache. Returns (status, busy): status is
    "ok" | "needs_permission" | "error". `busy` is the exact
    {"start": datetime, "end": datetime} shape place_slots already
    expects.

    writable_only mirrors list_events_window's own contract exactly: True
    for the scheduler (only calendars she can act on matter for
    placement) — this function is never called with False.

    NOTE: this is PROPOSE only. The pre-create RE-VERIFY in
    calendar_add_slots below is its OWN separate EventKit read, taken
    fresh on that turn — that re-verify is the entire safety story that
    makes proposing here safe even against a just-changed calendar; it is
    not touched or weakened by this function.
    """
    read = await ek_read.read_window(start_dt, end_dt, writable_only=writable_only)
    if read["status"] != "ok":
        return read["status"], []

    busy = []
    for ev in read["events"]:
        s = _parse_window_event_dt(ev.get("start"))
        e = _parse_window_event_dt(ev.get("end"))
        if s is not None and e is not None:
            busy.append({"start": s, "end": e})
    return "ok", busy


# ---------------------------------------------------------------------------
# Scheduler orchestration — composes EXISTING pieces (core.scheduler's
# calendar-blind infer_block + pure place_slots, plus a live calendar
# window read and create_calendar_event) into the propose half of the
# propose->accept->add loop. Deliberately lives HERE, not inside
# core/scheduler.py: that module has NO calendar/DB dependency at all
# (see its module docstring), and this function is exactly the seam where
# the model's activity reasoning and the deterministic calendar reasoning
# are handed off to each other — infer_block() and place_slots() never
# call each other directly, they only ever meet through this structured
# handoff.
# ---------------------------------------------------------------------------

def _parse_scheduler_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _parse_window_event_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _plan_result(status: str, activity: str, **extra) -> dict:
    result: dict = {"status": status, "kind": "plan", "activity": activity}
    result.update(extra)
    return result


async def _place_plan(
    activity: str, block: dict, window_start: datetime, window_end: datetime,
    count: int, tod_pref: str | None, day_exclusions: set,
) -> dict:
    """The shared placement TAIL of the scheduler pipeline: the PROPOSE
    step's busy-set read (live via EventKit — EK1, fast, no contention,
    no cache-staleness question) + place_slots, given an already-computed
    `block` dict. Used by BOTH _suggest_slots_plan (the original
    proposal, block from infer_block) and the correction re-plan in
    calendar_correct_plan (block re-inferred with a corrected override)
    — the only thing that differs between the two callers is how `block`
    was produced; the placement logic itself is identical and lives here
    exactly once, so the placer/re-verify behavior can't drift between
    the two paths.

    IMPORTANT: this is PROPOSE only. calendar_add_slots' pre-create
    RE-VERIFY takes its OWN separate EventKit read, fresh on that later
    turn — proposing here and re-verifying there are deliberately two
    separate reads, since time passes (and something could change)
    between a proposal and the user's yes.

    Never raises. The returned dict carries window_start/window_end/
    day_exclusions/requested back out (as plain ISO strings) so a client
    can hold them in its pending-plan state and pass them straight back
    unchanged on a later correction, without needing to re-derive the
    original request's window.
    """
    range_end = window_end.replace(hour=23, minute=59, second=59)
    busy_status, busy = await _ek_busy_events(window_start, range_end, writable_only=True)
    if busy_status == "needs_permission":
        return _plan_result("needs_permission", activity, message=_EK_PERMISSION_MESSAGE)
    if busy_status != "ok":
        return _plan_result("error", activity, message="Couldn't read the calendar.")

    window_days = []
    d = window_start.date()
    while d <= window_end.date():
        if d not in day_exclusions:
            window_days.append(d)
        d += timedelta(days=1)

    slots = place_slots(
        busy, block["block_min"], count, window_days,
        tod_pref=tod_pref, hours=(8, 22), now=datetime.now(),
    )

    status = "plan" if slots else "plan_empty"
    result = _plan_result(
        status, activity,
        block_min=block["block_min"],
        breakdown=block["breakdown"],
        padded=block["padded"],
        pad_before_min=block["pad_before_min"],
        pad_after_min=block["pad_after_min"],
        default_duration_min=block["default_duration_min"],
        tod_pref=tod_pref,
        slots=[{"start": s["start"].isoformat(), "end": s["end"].isoformat()} for s in slots],
        requested=count,
        fit=len(slots),
        window_start=window_start.strftime("%Y-%m-%d"),
        window_end=window_end.strftime("%Y-%m-%d"),
        day_exclusions=[d.isoformat() for d in sorted(day_exclusions)],
    )
    return result


async def _suggest_slots_plan(args: dict, user_id: str) -> dict:
    """Orchestrates one suggest_slots turn end to end:
      1. Gate on "list" (proposing only READS the calendar).
      2. stored_pref = fetch_scheduling_preference (S1) — numeric fields
         only; never notes (see THE notes WALL in core/db.py).
      3. block = infer_block(activity, stated_dur, stored_pref) — model,
         calendar-BLIND. It never sees the calendar.
      4. _place_plan — a live window read + place_slots (pure; can ONLY
         select from computed free gaps, so it is structurally incapable
         of proposing a double-book no matter what step 3 inferred).
    Never raises — every failure path returns a structured "plan" dict for
    the client to render; there is no bare exception path back to /chat.
    """
    activity = (args.get("activity") or "").strip()

    perms = await db.fetch_calendar_permissions(user_id)
    if not (perms["master"] and perms.get("list", False)):
        return _plan_result(
            "app_disabled", activity,
            message="I need to see your calendar to plan — turn on List in Settings.",
        )

    if not activity:
        return _plan_result("error", activity, message="Couldn't tell what to schedule.")

    window_start = _parse_scheduler_date(args.get("window_start"))
    window_end = _parse_scheduler_date(args.get("window_end"))
    if window_start is None or window_end is None or window_end < window_start:
        return _plan_result("error", activity, message="Couldn't tell what window to plan for.")

    try:
        count = max(1, int(args.get("count") or 1))
    except (TypeError, ValueError):
        count = 1

    requested_tod_pref = args.get("tod_pref") or None

    day_exclusions = set()
    for d in (args.get("day_exclusions") or []):
        dt = _parse_scheduler_date(d)
        if dt is not None:
            day_exclusions.add(dt.date())

    dmin = args.get("stated_duration_min")
    dmax = args.get("stated_duration_max")
    stated_dur = None
    if dmin and dmax:
        stated_dur = f"{dmin}-{dmax} min"
    elif dmin or dmax:
        stated_dur = f"{dmin or dmax} min"

    # S1 exists — wire it. Numeric fields only; fetch_scheduling_preference
    # never returns/selects `notes` toward anything but infer_block's own
    # breakdown phrasing, and infer_block itself never routes it further.
    stored_pref = await db.fetch_scheduling_preference(user_id, activity.strip().lower())

    block = await infer_block(activity, stated_dur, stored_pref)

    # The user's OWN stated tod_pref (parsed straight from their words by
    # the resolver) wins over infer_block's generic guess about the
    # activity — infer_block never sees the user's actual request text
    # (calendar-blind AND request-blind by design, see core/scheduler.py),
    # so its tod_pref is only ever a fallback for when the user didn't
    # state one themselves.
    effective_tod_pref = requested_tod_pref or block.get("tod_pref")

    return await _place_plan(
        activity, block, window_start, window_end, count, effective_tod_pref, day_exclusions,
    )


# ---------------------------------------------------------------------------
# Scheduler correction (S4) — interprets a padding/duration/time-of-day
# correction to a PENDING proposal, calendar-blind, exactly like every
# other follow-up reasoning step in this codebase (delete/move's
# resolveFollowUp is client-side and deterministic; this one genuinely
# needs a model call to parse free text like "I shower at home" into
# structured fields, so it lives server-side). Never writes anything —
# see calendar_correct_plan below for the write-on-accept step.
# ---------------------------------------------------------------------------

_CORRECTION_MODEL = "gemini-2.5-flash"
_CORRECTION_TIMEOUT = 15.0

_APPLY_CORRECTION = types.FunctionDeclaration(
    name="apply_correction",
    description="Apply a user's correction to a pending scheduling proposal's padding, core "
                "duration, or time-of-day",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "pad_before_min": types.Schema(
                type=types.Type.INTEGER,
                description="NEW pad_before_min in minutes (absolute, not a delta) — ONLY if the "
                            "correction changes prep/commute time before the activity"),
            "pad_after_min": types.Schema(
                type=types.Type.INTEGER,
                description="NEW pad_after_min in minutes (absolute, not a delta) — ONLY if the "
                            "correction changes cleanup/commute time after the activity"),
            "default_duration_min": types.Schema(
                type=types.Type.INTEGER,
                description="NEW core activity duration in minutes (absolute, not a delta) — ONLY "
                            "if the correction changes the activity's own length"),
            "tod_pref": types.Schema(
                type=types.Type.STRING,
                description="NEW time-of-day preference ('morning'/'afternoon'/'evening') — ONLY "
                            "if the correction changes it"),
            "echo": types.Schema(
                type=types.Type.STRING,
                description="Short first-person confirmation of exactly what was learned, e.g. "
                            "'got it — no shower time for gym from now on'"),
        },
        required=["echo"],
    ),
)
_CORRECTION_TOOL = types.Tool(function_declarations=[_APPLY_CORRECTION])


def _breakdown_text_for_prompt(breakdown: list) -> str:
    parts = []
    for b in breakdown:
        label = b.get("label") if isinstance(b, dict) else getattr(b, "label", None)
        minutes = b.get("minutes") if isinstance(b, dict) else getattr(b, "minutes", None)
        if label is not None and minutes is not None:
            parts.append(f"{minutes} min {label}")
    return ", ".join(parts) if parts else "(no breakdown)"


def _correction_system_instruction(activity: str, breakdown_text: str) -> str:
    return (
        f"The user is reacting to a scheduling proposal you already gave them for '{activity}'. "
        f"Its current time breakdown is: {breakdown_text}.\n\n"
        "Decide whether their message is a CORRECTION to THIS block's padding, core duration, or "
        "time-of-day preference — e.g. 'I shower at home' or 'I already showered' (they do that "
        "step somewhere this block doesn't need to cover — set pad_after_min to EXACTLY 0, not a "
        "smaller nonzero number), '1.5 is enough' (cap the core duration), 'skip the commute, I "
        "live next door' or 'no I don't need prep time' (they don't need that step at all — set "
        "pad_before_min to EXACTLY 0), 'mornings work better' (change time-of-day). A correction "
        "that says a step isn't needed AT ALL means the matching field becomes 0, not a reduced "
        "estimate of your own.\n\n"
        "If it IS such a correction: call apply_correction. Set ONLY the field(s) that actually "
        "change, each to its NEW absolute value (not a delta to add or subtract) — pad_before_min "
        "and/or pad_after_min in minutes if prep/cleanup time changes, default_duration_min in "
        "minutes if the core activity length changes, tod_pref if the time of day changes. Leave "
        "any field that doesn't change OUT of the call entirely. Always include a short first-"
        "person echo that matches the value you actually set — if you set a field to 0, the echo "
        "must say that step is skipped entirely ('no shower time'), never imply zero while leaving "
        "some nonzero minutes for it, e.g. "
        f"'got it — no shower time for {activity} from now on'.\n\n"
        "If it is NOT a correction to THIS block — a request for a different activity, a change to "
        "how many slots or which window, a totally unrelated message, a question, small talk, or "
        "anything genuinely ambiguous — do NOT call the function. Reply with the single word "
        "UNSUPPORTED. When unsure, prefer UNSUPPORTED: a missed correction just falls through to "
        "normal chat and is recoverable; a wrongly-guessed one gets remembered and reused next "
        "time, which is worse."
    )


async def _interpret_correction(activity: str, correction_text: str, breakdown: list) -> dict | None:
    """Calendar-blind: sees only the correction text and the CURRENT
    block's breakdown — never the calendar, never the slots, same
    invariant as infer_block. Returns a delta dict ({field: new_value,
    ..., "echo": str}, only the changed fields present) or None if this
    isn't a genuine correction — either the model explicitly declined
    (UNSUPPORTED, same pattern as the resolver) or the call itself
    failed. Callers MUST treat None as "not a correction, fall through to
    the escape hatch" — never as an empty-but-valid delta.
    """
    breakdown_text = _breakdown_text_for_prompt(breakdown)
    try:
        resp = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=_CORRECTION_MODEL,
                contents=[types.Content(
                    role="user", parts=[types.Part.from_text(text=correction_text)],
                )],
                config=types.GenerateContentConfig(
                    system_instruction=_correction_system_instruction(activity, breakdown_text),
                    tools=[_CORRECTION_TOOL],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.AUTO,
                        )
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    temperature=0,
                    safety_settings=SAFETY_SETTINGS,
                ),
            ),
            timeout=_CORRECTION_TIMEOUT,
        )
        fcs = resp.function_calls
        if not fcs:
            log.info("[scheduler] correction declined (not recognized as one) for %r: %r",
                      activity, correction_text)
            return None

        args = dict(fcs[0].args)
        delta: dict = {}
        for key in ("pad_before_min", "pad_after_min", "default_duration_min"):
            if key in args:
                try:
                    delta[key] = int(args[key])
                except (TypeError, ValueError):
                    pass
        if args.get("tod_pref") in ("morning", "afternoon", "evening"):
            delta["tod_pref"] = args["tod_pref"]

        if not delta:
            # The model called the function but changed nothing recognizable
            # — treat as "not actually a correction" rather than persisting
            # a no-op preference later.
            return None

        delta["echo"] = (str(args.get("echo") or "").strip()
                          or f"got it, I'll remember that for {activity}.")
        log.info("[scheduler] correction interpreted for %r: %s", activity, delta)
        return delta

    except Exception as exc:
        log.error("[scheduler] correction interpretation failed for %r: %s", activity, exc)
        return None


@app.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user_id: str = Depends(get_current_user),
):
    global _direct_vision_ok
    log.debug("Chat from user_id=%s username=%s", user_id, req.username)

    session = await get_session(user_id)

    async with session.lock:
        message_text = f"[{req.username}]: {req.message}"
        image_bytes  = base64.b64decode(req.screenshot) if req.screenshot else None

        # Description fallback: convert image to text before building parts.
        if image_bytes and not _direct_vision_ok:
            desc = await describe_screen(image_bytes)
            if desc:
                message_text += f"\n[Screen: {desc}]"
            image_bytes = None

        extra_parts = (
            [types.Part.from_bytes(data=image_bytes, mime_type="image/png")]
            if image_bytes and _direct_vision_ok
            else None
        )

        await session.append("user", message_text, extra_parts)
        await session.compress_rolling(gemini_client, MEMORY_MODEL)

        try:
            reply = await asyncio.wait_for(
                generate_reply(session.contents, build_dynamic_prompt(session)),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            # Remove the message we just appended — it never got a response.
            session.pop_last()
            raise HTTPException(status_code=504, detail="Gemini timed out.")
        except Exception as exc:
            # First multimodal failure: switch to description mode and retry once.
            if req.screenshot and _direct_vision_ok:
                log.warning("Direct vision rejected (%s) — switching to description fallback.", exc)
                _direct_vision_ok = False
                session.pop_last()

                desc = await describe_screen(base64.b64decode(req.screenshot))
                retry_text = f"[{req.username}]: {req.message}"
                if desc:
                    retry_text += f"\n[Screen: {desc}]"
                await session.append("user", retry_text)
                try:
                    reply = await asyncio.wait_for(
                        generate_reply(session.contents, build_dynamic_prompt(session)),
                        timeout=30.0,
                    )
                except Exception as retry_exc:
                    session.pop_last()
                    log.error("Retry failed: %s", retry_exc)
                    raise HTTPException(status_code=500, detail=str(retry_exc))
            else:
                session.pop_last()
                log.error("Generation error: %s", exc)
                raise HTTPException(status_code=500, detail=str(exc))

        if not reply:
            session.pop_last()
            log.error("Empty reply from model; rolled back user turn.")
            raise HTTPException(status_code=500, detail="No reply generated.")

        # Stage 1: detect + strip tool-action signal
        action_request: str | None = None
        calendar_action: dict | None = None
        m = re.search(r"<action>\s*calendar:\s*(.*?)\s*</action>", reply, re.IGNORECASE | re.DOTALL)
        if m:
            action_request = m.group(1).strip()
            reply = re.sub(r"<action>.*?</action>", "", reply, flags=re.IGNORECASE | re.DOTALL).strip()
            log.info("[tool-signal] calendar action requested: %r", action_request)
            try:
                resolved = await resolve_calendar_action(action_request)
                log.info("[stage2] result: status=%s tool=%s args=%r",
                         resolved.status, resolved.tool, resolved.args)
                # Stage 3: actually execute resolved calendar actions. Never
                # raises — a clean "error" result on failure, /chat still returns.
                # Permission is per-user (profiles.calendar_enabled master switch
                # plus a per-action grant, set via the Agency Permissions toggles
                # in Settings) — fetched fresh each time so a user flipping it
                # off takes effect on their very next message, not just after
                # some cache expires. Unmapped/future tools default to denied.
                if resolved.status == "resolved" and resolved.tool in (
                    "create_event", "list_events", "delete_event", "move_event",
                ):
                    perms = await db.fetch_calendar_permissions(user_id)
                    action_key = _CALENDAR_TOOL_PERMISSIONS.get(resolved.tool)
                    cal_enabled = perms["master"] and perms.get(action_key, False)
                    if resolved.tool == "create_event":
                        calendar_action = await create_calendar_event(resolved.args, enabled=cal_enabled)
                    elif resolved.tool == "list_events":
                        # EK1: the display read now hits EventKit directly,
                        # live — see the "Calendar reads" section above.
                        calendar_action = await _list_events_from_ek(
                            resolved.args, enabled=cal_enabled,
                        )
                    elif resolved.tool == "move_event":
                        calendar_action = await move_event(resolved.args, enabled=cal_enabled)
                    else:
                        calendar_action = await delete_event(resolved.args, enabled=cal_enabled)
                    log.info("[stage3] execution result (tool=%s calendar_enabled=%s): %s",
                             resolved.tool, cal_enabled, calendar_action)
                elif resolved.status == "resolved" and resolved.tool == "suggest_slots":
                    # Scheduler propose half — its own orchestrator (composes
                    # infer_block + a live window read + place_slots) fetches
                    # permissions itself since it gates on "list" rather than
                    # the per-tool map's default lookup used above.
                    calendar_action = await _suggest_slots_plan(resolved.args, user_id)
                    log.info("[stage3] suggest_slots execution result: %s", calendar_action)
            except Exception as exc:
                log.warning("[stage2/3] calendar tool pipeline failed (non-fatal): %s", exc)
        if not reply:
            reply = "on it!"

        await session.append("assistant", reply)
        asyncio.create_task(session.update_working_memory(gemini_client, MEMORY_MODEL))

        # TTS — non-fatal: missing voice or synthesis error just omits audio.
        # On list turns the reply is acknowledgment-only by design (see the
        # OOC TOOL SIGNALING rules in core/prompts.py) and chat.html already
        # suppresses it from the screen if the model slips and narrates a
        # schedule anyway — mirror that here so the fake schedule can't be
        # SPOKEN either. A fixed phrase is used instead of the model's raw
        # reply precisely because it can't carry fabricated event content.
        # A suggest_slots ("plan") turn is the same phantom-bug shape: the
        # reply is written BEFORE the tool runs, so it can't know the real
        # slots/breakdown either — same fence, same fixed phrase.
        audio_b64: str | None = None
        try:
            tts_ref = await db.fetch_voice_tts_ref(user_id)
            if tts_ref:
                is_list_turn = calendar_action is not None and calendar_action.get("kind") in ("list", "plan")
                tts_text = "let me check" if is_list_turn else reply
                audio_bytes = await tts.synthesize(tts_text, tts_ref)
                if audio_bytes:
                    audio_b64 = base64.b64encode(audio_bytes).decode()
        except Exception as exc:
            log.warning("TTS failed (non-fatal): %s", exc)

        return ChatResponse(
            reply=reply,
            working_memory={
                "location": session.working_memory.location,
                "activity": session.working_memory.activity,
                "mood":     session.working_memory.mood,
            },
            audio_b64=audio_b64,
            calendar_action=calendar_action,
        )


@app.post("/calendar/delete_by_uid")
async def calendar_delete_by_uid(
    req: DeleteByUidRequest,
    user_id: str = Depends(get_current_user),
):
    """Guarded delete of an already-known uid+calendar — used by the client
    to resolve a follow-up disambiguation ('the 6pm one' / 'yes, delete it')
    against a PRIOR delete_event() "disambiguate" result (candidates the
    user is choosing between, held client-side, not re-derived here).

    Runs the exact same guards as delete_event()'s auto-act path
    (recurrence refusal, re-verify, delete+confirm) via the shared
    delete_by_uid() — there is no separate, unguarded delete path in this
    module. Permission is fetched fresh on this turn rather than trusting
    whatever was true on the original disambiguating turn, same as every
    other calendar action.
    """
    perms = await db.fetch_calendar_permissions(user_id)
    enabled = perms["master"] and perms.get("delete", False)
    result = await delete_by_uid(req.uid, req.calendar, enabled=enabled)
    log.info("[calendar] delete_by_uid uid=%s calendar=%r enabled=%s -> %s",
             req.uid, req.calendar, enabled, result)
    return result


@app.post("/calendar/move_by_uid")
async def calendar_move_by_uid(
    req: MoveByUidRequest,
    user_id: str = Depends(get_current_user),
):
    """Guarded move of an already-known uid+calendar — used by the client
    to resolve a follow-up disambiguation ('the 6pm one' / 'yes, move it')
    against a PRIOR move_event() "disambiguate" result, carrying the
    originally-requested new_start/new_end alongside the chosen uid (held
    client-side, not re-derived here).

    Runs the exact same guards as move_event()'s auto-act path (recurrence
    refusal, re-verify, safe-ordered write, confirm read-back) via the
    shared move_by_uid() — there is no separate, unguarded move path in
    this module. Permission is fetched fresh on this turn rather than
    trusting whatever was true on the original disambiguating turn, same
    as every other calendar action.
    """
    perms = await db.fetch_calendar_permissions(user_id)
    enabled = perms["master"] and perms.get("move", False)
    result = await move_by_uid(req.uid, req.calendar, req.new_start, req.new_end, enabled=enabled)
    log.info("[calendar] move_by_uid uid=%s calendar=%r new_start=%r enabled=%s -> %s",
             req.uid, req.calendar, req.new_start, enabled, result)
    return result


@app.post("/calendar/add_slots")
async def calendar_add_slots(
    req: AddSlotsRequest,
    user_id: str = Depends(get_current_user),
):
    """Accept half of the scheduler propose->accept->add loop — the client
    calls this after the user says yes to a suggest_slots proposal. Gated
    on "create" (this WRITES to the calendar), independently of the
    "list" gate the proposal itself used, since Add can be off while List
    stays on (or vice versa).

    RE-VERIFIES before writing anything — the load-bearing guard, the
    batch equivalent of delete/move's staleness re-verify: time has
    passed since the proposal was built (however the user's yes took to
    arrive), and something else could have filled a proposed slot since.
    A fresh EventKit read (EK1 — core.calendar_read_ek, expands recurring
    occurrences, so a recurring class landing in a proposed slot is
    caught here too) covering every proposed slot is taken HERE, on this
    turn, and any slot that now conflicts is skipped rather than
    clobbered — create_calendar_event is only ever called on slots
    confirmed still free by this fresh read, never on the stale slots
    the client sent.
    """
    perms = await db.fetch_calendar_permissions(user_id)
    enabled = perms["master"] and perms.get("create", False)
    if not enabled:
        return {
            "status": "app_disabled", "kind": "plan_added",
            "message": "found slots, but I can't add them — turn on Add in Settings.",
            "added": [], "skipped": [],
        }

    activity = (req.activity or "").strip()
    parsed_slots = []
    for s in req.slots:
        start = _parse_window_event_dt(s.start)
        end = _parse_window_event_dt(s.end)
        if start is not None and end is not None and end > start:
            parsed_slots.append((start, end))

    if not activity or not parsed_slots:
        return {
            "status": "error", "kind": "plan_added",
            "message": "Nothing to add.", "added": [], "skipped": [],
        }

    window_start = min(s for s, _ in parsed_slots)
    window_end = max(e for _, e in parsed_slots)
    fresh = await ek_read.read_window(window_start, window_end, writable_only=True)
    if fresh["status"] == "needs_permission":
        return {
            "status": "needs_permission", "kind": "plan_added",
            "message": _EK_PERMISSION_MESSAGE,
            "added": [], "skipped": [],
        }
    if fresh["status"] != "ok":
        # Never fall back to an empty busy set on a failed re-verify read —
        # that would silently defeat the whole point of re-verifying, by
        # treating "couldn't check" the same as "confirmed free".
        return {
            "status": "error", "kind": "plan_added",
            "message": "Couldn't re-check your calendar before adding — try again.",
            "added": [], "skipped": [],
        }

    fresh_busy = []
    for ev in fresh["events"]:
        s = _parse_window_event_dt(ev.get("start"))
        e = _parse_window_event_dt(ev.get("end"))
        if s is not None and e is not None:
            fresh_busy.append((s, e))

    def _conflicts(start: datetime, end: datetime) -> bool:
        return any(start < b_end and end > b_start for b_start, b_end in fresh_busy)

    added, skipped = [], []
    for start, end in parsed_slots:
        slot_out = {"start": start.isoformat(), "end": end.isoformat()}
        if _conflicts(start, end):
            skipped.append(slot_out)
            continue
        result = await create_calendar_event(
            {"title": activity, "start": start.isoformat(), "end": end.isoformat()}, enabled=enabled,
        )
        if result.get("status") == "success":
            added.append(slot_out)
        else:
            skipped.append({**slot_out, "reason": result.get("status")})

    status = "success" if not skipped else "partial"

    # Write-on-accept (S4): a pref_delta is only ever present because the
    # user already said yes to a CORRECTED plan carrying it — this is the
    # one and only place a preference gets written. Reject/drop/escape
    # never reach here at all, so there is no other path that persists
    # anything. Happens AFTER the slots are added, and regardless of
    # per-slot skips — the user's acceptance of the correction itself is
    # independent of whether an individual slot happened to fill up in
    # the interim. Structured fields only; `echo` (if present) lands in
    # `notes` for later display ONLY — see THE notes WALL in core/db.py,
    # it never reaches the persona prompt.
    pref_saved = False
    if req.pref_delta is not None:
        fields = {}
        for key in ("pad_before_min", "pad_after_min", "default_duration_min", "tod_pref"):
            value = getattr(req.pref_delta, key)
            if value is not None:
                fields[key] = value
        if req.pref_delta.echo:
            fields["notes"] = req.pref_delta.echo
        if fields:
            pref_saved = await db.upsert_scheduling_preference(
                user_id, activity.strip().lower(), fields,
            )

    log.info("[calendar] add_slots activity=%r added=%d skipped=%d pref_saved=%s",
             activity, len(added), len(skipped), pref_saved)
    return {
        "status": status, "kind": "plan_added", "activity": activity,
        "added": added, "skipped": skipped, "pref_saved": pref_saved,
    }


@app.post("/calendar/correct_plan")
async def calendar_correct_plan(
    req: CorrectPlanRequest,
    user_id: str = Depends(get_current_user),
):
    """Interprets a padding/duration/time-of-day correction to a PENDING
    proposal and re-plans — never writes anything (see calendar_add_slots
    for the write-on-accept step). Gated on "list" — same gate as the
    original proposal, since this only READS the calendar again for the
    re-plan; nothing is written here.

    Client contract: the client calls this speculatively whenever a
    message doesn't look like yes/no/a day-drop while a plan is pending.
    If the model doesn't recognize the text as a genuine correction, this
    returns {"status": "not_a_correction", "kind": "plan"} and the client
    falls through to its normal escape hatch (clear pendingPlan, route to
    ordinary chat) — this endpoint never guesses; see
    _interpret_correction's docstring for why an ambiguous message must
    resolve to "not a correction," not a guessed delta.
    """
    perms = await db.fetch_calendar_permissions(user_id)
    if not (perms["master"] and perms.get("list", False)):
        return _plan_result(
            "app_disabled", req.activity,
            message="I need to see your calendar to plan — turn on List in Settings.",
        )

    breakdown = [b.model_dump() for b in req.breakdown]
    delta = await _interpret_correction(req.activity, req.correction_text, breakdown)
    if delta is None:
        return {"status": "not_a_correction", "kind": "plan", "activity": req.activity}

    echo = delta.pop("echo")

    window_start = _parse_scheduler_date(req.window_start)
    window_end = _parse_scheduler_date(req.window_end)
    if window_start is None or window_end is None or window_end < window_start:
        return _plan_result("error", req.activity, message="Couldn't tell what window to plan for.")

    day_exclusions = set()
    for d in (req.day_exclusions or []):
        dt = _parse_scheduler_date(d)
        if dt is not None:
            day_exclusions.add(dt.date())

    # The corrected override passed to infer_block carries the FULL
    # current state (not just the delta) — infer_block's documented
    # contract is "stored_pref overrides for any field it lists," so
    # listing every padding/duration field here (delta applied on top)
    # makes the re-plan reflect the correction exactly, not a mix of the
    # correction plus a fresh, possibly-different model guess for the
    # unchanged fields.
    corrected_pref = {
        "pad_before_min": req.pad_before_min,
        "pad_after_min": req.pad_after_min,
        "default_duration_min": req.default_duration_min,
    }
    if req.tod_pref:
        corrected_pref["tod_pref"] = req.tod_pref
    corrected_pref.update(delta)

    block = await infer_block(req.activity, None, corrected_pref)
    effective_tod_pref = corrected_pref.get("tod_pref") or block.get("tod_pref")

    plan = await _place_plan(
        req.activity, block, window_start, window_end, req.count, effective_tod_pref, day_exclusions,
    )
    plan["echo"] = echo
    # The delta (numeric fields only) rides along so the client can STAGE
    # it on pendingPlan and send it back unchanged to /calendar/add_slots
    # if/when this corrected plan is accepted — nothing is written here.
    plan["pref_delta"] = {**delta, "echo": echo}
    log.info("[calendar] correct_plan activity=%r delta=%s -> status=%s fit=%s",
              req.activity, delta, plan.get("status"), plan.get("fit"))
    return plan


# ---------------------------------------------------------------------------
# Scheduling preferences see/reset valve (S5). Touches NO calendar — these
# two endpoints read/write scheduling_preferences only, so unlike every
# calendar-facing endpoint above there is no fetch_calendar_permissions
# gate here at all; RLS + scoping every query to the verified token's
# user_id is the only guard, same as any other purely-account-scoped data.
# Never renders `notes` back for display purposes beyond what the client
# does with it — see chat.html's render, which builds its plain-language
# summary from the NUMERIC fields only and never surfaces notes.
# ---------------------------------------------------------------------------

class ResetSchedulingPrefsRequest(BaseModel):
    activity: str | None = None


@app.get("/scheduling_prefs")
async def get_scheduling_prefs(user_id: str = Depends(get_current_user)):
    """Every learned scheduling preference for the CALLING user only —
    list_scheduling_preferences (S1) already scopes its query to
    user_id, so there is no path here that could return another user's
    rows. `notes` is stripped out of the response entirely: the client
    render must never depend on it (see THE notes WALL in core/db.py) —
    removing it here makes that impossible by construction, not just by
    convention on the client.
    """
    rows = await db.list_scheduling_preferences(user_id)
    prefs = [{k: v for k, v in row.items() if k != "notes"} for row in rows]
    return {"status": "ok", "prefs": prefs}


@app.post("/scheduling_prefs/reset")
async def reset_scheduling_prefs(
    req: ResetSchedulingPrefsRequest,
    user_id: str = Depends(get_current_user),
):
    """Reset one learned preference (`activity` given) or ALL of them
    (`activity` omitted/None) for the CALLING user only —
    delete_scheduling_preference (S1) scopes its query to user_id, so a
    reset can never touch another user's rows regardless of what
    `activity` is passed. The client is expected to confirm with the
    user BEFORE calling this (per-activity or "clear everything," both
    listing exactly what's being wiped) — this endpoint itself performs
    no confirmation of its own, it trusts the caller already got one.
    """
    activity = (req.activity or "").strip() or None
    cleared_before = await db.list_scheduling_preferences(user_id)
    cleared = [
        row["activity"] for row in cleared_before
        if activity is None or row["activity"] == activity
    ]

    ok = await db.delete_scheduling_preference(user_id, activity)
    log.info("[scheduling_prefs] reset user=%s activity=%r ok=%s cleared=%s",
             user_id, activity, ok, cleared)
    return {"status": "ok" if ok else "error", "cleared": cleared if ok else []}


@app.post("/nudge", response_model=NudgeResponse)
async def nudge(req: NudgeRequest, user_id: str = Depends(get_current_user)):
    from core.prompts import NUDGE_INSTRUCTION_TEMPLATE, build_proactive_prompt
    global _direct_vision_ok

    if not _nudge_allowed(user_id):
        raise HTTPException(status_code=429, detail="Nudge limit reached.")

    session = await get_session(user_id)
    async with session.lock:
        ctx = req.context or ""
        image_bytes = base64.b64decode(req.screenshot) if req.screenshot else None

        if image_bytes and not _direct_vision_ok:
            desc = await describe_screen(image_bytes)
            if desc:
                ctx += f"\n[Screen: {desc}]"
            image_bytes = None

        instruction = NUDGE_INSTRUCTION_TEMPLATE.format(username=req.username, context=ctx)
        parts = ([types.Part.from_bytes(data=image_bytes, mime_type="image/png")]
                 if image_bytes and _direct_vision_ok else [])
        parts.append(types.Part.from_text(text=instruction))
        contents = session.contents + [types.Content(role="user", parts=parts)]

        try:
            message = await asyncio.wait_for(
                generate_reply(contents, build_proactive_prompt(session, ctx), grounding=False),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Gemini timed out.")
        except Exception as exc:
            log.error("Nudge generation error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        if not message:
            raise HTTPException(status_code=500, detail="No nudge generated.")

        await session.append("assistant", message)
        _nudge_record(user_id)
        asyncio.create_task(session.compress_rolling(gemini_client, MEMORY_MODEL))
        asyncio.create_task(session.update_working_memory(gemini_client, MEMORY_MODEL))

        audio_b64: str | None = None
        try:
            tts_ref = await db.fetch_voice_tts_ref(user_id)
            if tts_ref:
                audio_bytes = await tts.synthesize(message, tts_ref)
                if audio_bytes:
                    audio_b64 = base64.b64encode(audio_bytes).decode()
        except Exception as exc:
            log.warning("TTS failed (non-fatal): %s", exc)

        return NudgeResponse(
            message=message,
            working_memory={
                "location": session.working_memory.location,
                "activity": session.working_memory.activity,
                "mood":     session.working_memory.mood,
            },
            audio_b64=audio_b64,
        )


@app.get("/profile")
async def get_profile(user_id: str = Depends(get_current_user)):
    cfg = await db.fetch_avatar_config(user_id)
    return {"avatar_file_path": cfg["file_path"], "expression_map": cfg["expression_map"]}


@app.get("/memory", response_model=MemoryResponse)
async def get_memory(user_id: str = Depends(get_current_user)):
    session = await get_session(user_id)
    return MemoryResponse(
        long_term=session.long_term_memory,
        working={
            "location": session.working_memory.location,
            "activity": session.working_memory.activity,
            "mood":     session.working_memory.mood,
        },
    )


@app.post("/sleep")
async def sleep(user_id: str = Depends(get_current_user)):
    """Compress the caller's current session into long-term memory."""
    session = await get_session(user_id)
    async with session.lock:
        await session.flush(gemini_client, MEMORY_MODEL)
    return {"status": "memory saved"}
