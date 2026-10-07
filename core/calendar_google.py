"""
Google Calendar READ + WRITE provider (G2a) — a swappable alternative to
core/calendar_read_ek.py (reads) and core/calendar_executor.py (writes),
built and tested standalone; NOT wired into the live app yet (G2b flips
the default, a later change). The read half mirrors calendar_read_ek's
exact event-dict shape {uid, title, start, end, all_day, calendar}; the
write half (create_event/move_event/move_by_uid/delete_event/
delete_by_uid/find_candidates) mirrors calendar_executor's exact result
shapes (ExecutionResult.to_dict() for create, _delete_result/_move_result
for delete/move, _to_candidate for candidates — all REUSED directly from
calendar_executor, not reimplemented, so there is zero chance of shape
drift between the two providers) so api/routes.py and chat.html need no
changes at cutover.

Unlike EventKit (single local machine, one implicit user), this is
multi-user: every call takes a `creds` object built from ONE user's stored
refresh token (core.db.fetch_google_refresh_token) plus app-level
client_id/client_secret/token_uri (.env constants — NOT per-user, the same
one-app-secret-many-users'-own-tokens pattern the ElevenLabs/Gemini keys
already use). This module does not import core.db itself — callers fetch
the refresh token and hand it in via _build_credentials, keeping this
module's only real dependency the Google API client, mirroring calendar_
read_ek's own "no Supabase" boundary.

Probe findings (G1 spike) this module is built on:
  - Scopes calendar.events + calendar.readonly cover event read/write AND
    calendarList enumeration — no broader `calendar` scope needed.
  - events.list(singleEvents=True, orderBy=startTime) expands recurring
    events into real per-occurrence instances, each with its own `id` and
    a `recurringEventId` linking back to the series master — confirmed
    8/8 recurring classes + the daily event, at correct per-occurrence
    times.
  - calendarList.list's `accessRole` (owner/writer vs reader) reproduces
    the writable/read-only split the app already relies on.
  - events.list's timeMin/timeMax match on OVERLAP, not start-time, and
    "primary" is an ALIAS for the user's own calendar (already present
    under its own real id in calendarList.list) — querying both would
    double-count that calendar's events under identical ids. This module
    only ever queries real calendarList ids, never "primary", and
    dedupes by instance id defensively regardless of cause.

NO-TOKEN / AUTH FAILURE: mirrors EventKit's needs_permission principle — a
missing or invalid connection returns {"status": "needs_connection",
"events": []} (reads) or {"status": "needs_connection", ...} (writes),
NEVER a bare empty "ok"/success. A connection problem must never read as
"nothing on your calendar". `creds=None` (no stored token at all) and a
revoked/invalid refresh token both land here identically, everywhere in
this module.

WRITE SURFACE (this half, built on the G1 probe's write findings):
  - create_event TARGETS a specific calendarId, defaulting to "primary"
    (the user's own Google calendar) when none is given — this IS the
    write-targeting fix: events land on Google, not silently on whatever
    iCloud calendar osascript's `first calendar whose writable is true`
    happened to pick. A per-user default-target-calendar preference is a
    follow-on (needs a settings picker); "primary" is the default now.
  - RECURRENCE REFUSAL is kept for parity with calendar_executor — move/
    delete both refuse when the target is part of a series (detected via
    recurringEventId on an instance, or a recurrence rule on a master).
    The probe proved single-instance editing works cleanly; lifting this
    refusal to use it is G4's deliberate, separate decision, not this
    one's.
  - SNAG #4 — DELETE CONFIRMATION: events.delete leaves the event
    retrievable by GET for a while afterward with status:"cancelled" —
    that is NOT "still there". The only honest "is it really gone" check
    is its ABSENCE from an events.list query (which excludes cancelled
    events by default), so delete confirmation here always re-reads via
    list, never trusts a GET-by-id or a bare recount.
  - RE-VERIFY: immediately before any destructive write, the target is
    re-read via a fresh GET — the same staleness guard calendar_executor
    runs before every delete/move, just collapsed into one API call
    instead of osascript's separate recurrence-check + uid-count scripts.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build as _build_service
from googleapiclient.errors import HttpError

from core.calendar_executor import (
    ExecutionResult, _delete_result, _move_result, _to_candidate,
    _normalize_title, _titles_match, _osa_distance, _approx_threshold,
    _resolve_delete_window, _parse_dt, _DEFAULT_DURATION,
)

log = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _build_credentials(refresh_token: str) -> Credentials:
    """Builds a Credentials object from a user's stored refresh token plus
    the app-level OAuth client (GOOGLE_OAUTH_CLIENT_ID/SECRET in .env,
    confirmed working in the G1 probe) — the googleapiclient auto-refreshes
    the short-lived access token from this on first use. Never raises on
    construction itself (no network call here); an invalid/revoked token
    only surfaces as a RefreshError once something actually calls the API,
    which read_window/enumerate_calendars already catch."""
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=_SCOPES,
    )


async def _run(fn):
    return await asyncio.to_thread(fn)


def _enumerate_calendars_sync(creds: Credentials | None) -> list[dict] | None:
    """Returns None (NOT []) specifically on "couldn't authenticate" (no
    creds, or a revoked/invalid token) so read_window can tell that apart
    from "genuinely talked to Google and got zero calendars" — the same
    distinction EventKit's permission_status() exists to make."""
    if creds is None:
        return None
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        resp = service.calendarList().list().execute()
        items = resp.get("items", [])
        return [
            {
                "id": c["id"],
                "title": c.get("summary", c["id"]),
                "writable": c.get("accessRole") in ("owner", "writer"),
            }
            for c in items
        ]
    except RefreshError as exc:
        log.info("[google-read] refresh token invalid/revoked: %s", exc)
        return None
    except Exception as exc:
        log.error("[google-read] enumerate_calendars crashed: %s", exc)
        return None


async def enumerate_calendars(creds: Credentials | None) -> list[dict]:
    """[{"id": str, "title": str, "writable": bool}, ...] for every Google
    calendar this user can see — never raises, [] on any failure
    (including no/invalid creds). Callers needing to distinguish "no
    calendars" from "couldn't authenticate" should go through read_window,
    which surfaces that distinction via needs_connection."""
    result = await _run(lambda: _enumerate_calendars_sync(creds))
    return result or []


def _parsed_start_end(value: dict) -> tuple[str | None, bool]:
    """Google's start/end is {"dateTime": "...-04:00"} for timed events or
    {"date": "YYYY-MM-DD"} for all-day ones. Returns (local-naive ISO
    string, is_all_day). A timed value is normalized to the SYSTEM's
    actual local zone before stripping the offset (mirrors the old CS3
    _snapshot_ts_to_local_iso pattern) rather than blindly trusting
    whatever offset the event happened to carry — robust even if an event
    was created by an organizer in a different timezone."""
    if "date" in value:
        try:
            d = datetime.strptime(value["date"], "%Y-%m-%d")
            return d.isoformat(timespec="seconds"), True
        except ValueError:
            return None, True
    dt_str = value.get("dateTime")
    if not dt_str:
        return None, False
    try:
        aware = datetime.fromisoformat(dt_str)
        local = aware.astimezone().replace(tzinfo=None)
        return local.isoformat(timespec="seconds"), False
    except ValueError:
        return None, False


def _read_one_calendar_sync(creds: Credentials, cal_id: str, cal_title: str, time_min: str, time_max: str) -> list[dict]:
    """One calendar, one synchronous call chain (paginated) — the unit
    read_window fans out across calendars via asyncio.to_thread, mirroring
    the old concurrent per-calendar osascript read's shape (N calendars,
    launched together, bounded by the slowest one) even though the
    underlying mechanism here is threads waiting on HTTP, not subprocesses.
    Never raises to the caller — any failure for THIS calendar degrades to
    "no events from it", not a crash of the whole window read; a total
    auth failure is caught earlier by enumerate_calendars instead."""
    events: list[dict] = []
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        page_token = None
        while True:
            resp = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            ).execute()
            for ev in resp.get("items", []):
                start_val, is_all_day = _parsed_start_end(ev.get("start", {}))
                end_val, _ = _parsed_start_end(ev.get("end", {}))
                events.append({
                    "uid": ev["id"],
                    "title": ev.get("summary", ""),
                    "start": start_val,
                    "end": end_val,
                    "all_day": is_all_day,
                    # The ADDRESSING token, not a display label — same
                    # convention calendar_read_ek's "calendar" already is:
                    # chat.html only ever round-trips candidate.calendar
                    # straight into delete_by_uid/move_by_uid, never
                    # renders it. For osascript that token IS the
                    # calendar's name (AppleScript addresses by name); for
                    # Google it MUST be the real calendarId — "Family" the
                    # display title and the id Google actually needs are
                    # two different strings, and only the id works for a
                    # later events.get/patch/delete call. Confirmed via a
                    # real create->find_candidates->delete round trip that
                    # using the title here 404s.
                    "calendar": cal_id,
                })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        log.warning("[google-read] calendar %r unreachable this read: %s", cal_title, exc)
    return events


async def read_window(
    creds: Credentials | None, start_dt: datetime, end_dt: datetime, writable_only: bool,
) -> dict:
    """THE replacement read, Google-backed: events.list(singleEvents=True)
    expands recurring events into per-occurrence instances (the entire
    point — see module docstring). Never raises. Returns one of:

      {"status": "needs_connection", "events": []}
        No stored refresh token (creds=None) or it's invalid/revoked.
        NEVER a bare empty "ok" — same principle as EventKit's
        needs_permission: a connection problem must never masquerade as
        "nothing on your calendar".

      {"status": "ok", "events": [{uid, title, start, end, all_day,
       calendar}, ...]}
        start/end are LOCAL-naive ISO strings at the OCCURRENCE's actual
        time (never the recurring master's) — the identical convention
        calendar_read_ek.read_window uses, so this is a drop-in swap at
        the I/O layer.

    writable_only=True filters to accessRole in (owner, writer), matching
    calendar_read_ek's writable_only contract exactly (so the scheduler's
    busy-set and add_slots' re-verify would need no changes to use this
    instead, once G2b repoints them).

    Per-calendar reads are fanned out concurrently (asyncio.gather over
    asyncio.to_thread), mirroring the old concurrent osascript read's
    shape — wall-clock bounded by the slowest calendar, not the sum.
    Results are deduped by instance id before returning (see module
    docstring — "primary" aliasing a real calendarList id is the known
    cause, guarded against directly by never querying "primary", but the
    dedup stays as a cheap defensive backstop).
    """
    cals = await _run(lambda: _enumerate_calendars_sync(creds))
    if cals is None:
        return {"status": "needs_connection", "events": []}

    targets = [c for c in cals if not writable_only or c["writable"]]
    if not targets:
        # writable_only=True with genuinely no writable calendars is a
        # real, correct empty — not a failure to distinguish from one.
        return {"status": "ok", "events": []}

    # Naive-local datetimes, same convention every other read_window
    # caller already uses -- .astimezone() on a naive value presumes
    # system-local time and attaches the correct (DST-aware) offset.
    time_min = start_dt.astimezone().isoformat()
    time_max = end_dt.astimezone().isoformat()

    per_cal_results = await asyncio.gather(*(
        asyncio.to_thread(_read_one_calendar_sync, creds, c["id"], c["title"], time_min, time_max)
        for c in targets
    ))

    seen: set[str] = set()
    deduped: list[dict] = []
    for events in per_cal_results:
        for ev in events:
            if ev["uid"] in seen:
                continue
            seen.add(ev["uid"])
            deduped.append(ev)

    return {"status": "ok", "events": deduped}


# ---------------------------------------------------------------------------
# WRITE SURFACE (G2b-the-write-half of G2a) — create/move/delete + the
# candidate search they share. See the module docstring's "WRITE SURFACE"
# section for the targeting/recurrence-refusal/snag-#4/re-verify rationale.
# Every result dict is built via calendar_executor's OWN ExecutionResult/
# _delete_result/_move_result/_to_candidate — reused directly, not
# reimplemented, so this provider's output is shape-identical to the
# osascript executor's by construction, not by careful copying.
# ---------------------------------------------------------------------------

_RECURRING_MOVE_MSG = "I can't move repeating events yet — change it in the Calendar app directly"
_RECURRING_DELETE_MSG = "I can't delete repeating events yet — remove it in the Calendar app directly"


def _get_event_sync(creds: Credentials | None, calendar_id: str, event_id: str) -> tuple[str, dict]:
    """status: "ok" | "not_found" | "needs_connection" | "error"."""
    if creds is None:
        return "needs_connection", {}
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        ev = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return "ok", ev
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during get: %s", exc)
        return "needs_connection", {}
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return "not_found", {}
        log.warning("[google-write] get event failed: %s", exc)
        return "error", {}
    except Exception as exc:
        log.error("[google-write] get event crashed: %s", exc)
        return "error", {}


async def _get_event(creds, calendar_id: str, event_id: str) -> tuple[str, dict]:
    return await _run(lambda: _get_event_sync(creds, calendar_id, event_id))


def _is_recurring(ev: dict) -> bool:
    """An event is part of a series if it's ONE OCCURRENCE of one
    (recurringEventId set) or IS the series master (its own recurrence
    rule set) — Google's JSON response is structurally reliable here
    (unlike AppleScript's `recurrence of evt`, which could raise inside
    the script and need a READ_ERROR sentinel), so there's no unreadable-
    property case to fail-safe against directly. The equivalent fail-safe
    is structural: every caller only reaches this after a successful
    ("ok") _get_event — any fetch/parse failure already refuses the
    write via that status check, before _is_recurring is ever called."""
    return bool(ev.get("recurringEventId")) or bool(ev.get("recurrence"))


def _patch_event_sync(creds: Credentials, calendar_id: str, event_id: str, body: dict) -> tuple[str, dict]:
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        ev = service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
        return "ok", ev
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during patch: %s", exc)
        return "needs_connection", {}
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return "not_found", {}
        log.warning("[google-write] patch event failed: %s", exc)
        return "error", {}
    except Exception as exc:
        log.error("[google-write] patch event crashed: %s", exc)
        return "error", {}


async def _patch_event(creds, calendar_id: str, event_id: str, body: dict) -> tuple[str, dict]:
    return await _run(lambda: _patch_event_sync(creds, calendar_id, event_id, body))


def _delete_event_raw_sync(creds: Credentials, calendar_id: str, event_id: str) -> str:
    """"ok" | "not_found" | "needs_connection" | "error". This is the raw
    DELETE call only — it does NOT by itself confirm the event is gone
    (see _confirm_gone_sync for why a GET right after this can still say
    otherwise)."""
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return "ok"
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during delete: %s", exc)
        return "needs_connection"
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return "not_found"
        log.warning("[google-write] delete event failed: %s", exc)
        return "error"
    except Exception as exc:
        log.error("[google-write] delete event crashed: %s", exc)
        return "error"


async def _delete_event_raw(creds, calendar_id: str, event_id: str) -> str:
    return await _run(lambda: _delete_event_raw_sync(creds, calendar_id, event_id))


def _confirm_gone_sync(
    creds: Credentials, calendar_id: str, event_id: str, start_dt: datetime, end_dt: datetime,
) -> bool:
    """SNAG #4 — the real post-delete "is it gone" check: events.list
    over a window bracketing the event's own original time (default
    showDeleted=False already excludes cancelled events), checking the
    deleted id is ABSENT from the results. Deliberately NOT a GET-by-id:
    that can still return 200 with status:"cancelled" for a while after a
    genuinely successful delete, which would false-fail this check if
    treated as "still there". On any exception, fails safe toward "not
    confirmed" — never report a delete as successful on a guess."""
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        time_min = (start_dt - timedelta(hours=2)).astimezone().isoformat()
        time_max = (end_dt + timedelta(hours=2)).astimezone().isoformat()
        resp = service.events().list(
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max, singleEvents=True,
        ).execute()
        ids = {item["id"] for item in resp.get("items", [])}
        return event_id not in ids
    except Exception as exc:
        log.warning("[google-write] confirm-gone check failed for uid=%s, assuming NOT confirmed: %s", event_id, exc)
        return False


async def find_candidates(title_fragment: str, date_str: str | None, creds: Credentials | None) -> tuple[str, str, list]:
    """Google equivalent of calendar_executor._find_candidates: the SAME
    exact/approximate matching logic, reused directly (byte-identical
    behavior — _normalize_title/_titles_match/_osa_distance/
    _approx_threshold are pure functions with zero osascript dependency),
    over this module's OWN read instead of the concurrent osascript scan.
    writable_only=True always — a read-only calendar's event must never
    be offered as a delete/move candidate. Returns (status, tier,
    candidates): status is "ok" | "needs_connection" | "error"; tier is
    "exact" | "approx" | "none" (meaningful only when status == "ok")."""
    if creds is None:
        return "needs_connection", "none", []

    start_dt, end_dt = _resolve_delete_window(date_str)
    read = await read_window(creds, start_dt, end_dt, writable_only=True)
    if read["status"] == "needs_connection":
        return "needs_connection", "none", []
    if read["status"] != "ok":
        return "error", "none", []
    events = read["events"]

    query_norm = _normalize_title(title_fragment)
    exact = [
        _to_candidate(ev) for ev in events
        if _titles_match(query_norm, _normalize_title(ev.get("title", "")))
    ]
    if exact:
        return "ok", "exact", exact

    threshold = _approx_threshold(query_norm)
    scored = []
    for ev in events:
        ev_norm = _normalize_title(ev.get("title", ""))
        if not ev_norm:
            continue
        dist = _osa_distance(query_norm, ev_norm)
        if dist <= threshold:
            scored.append((dist, _to_candidate(ev)))
    if scored:
        scored.sort(key=lambda pair: pair[0])
        return "ok", "approx", [c for _, c in scored]

    return "ok", "none", []


def _event_body(start_dt: datetime, end_dt: datetime, all_day: bool) -> dict:
    if all_day:
        return {
            "start": {"date": start_dt.strftime("%Y-%m-%d")},
            "end": {"date": end_dt.strftime("%Y-%m-%d")},
        }
    # .astimezone() on a naive datetime presumes system-local time and
    # attaches the correct (DST-aware) offset directly in the ISO string —
    # no separate timeZone field needed, and no hardcoded zone name.
    return {
        "start": {"dateTime": start_dt.astimezone().isoformat()},
        "end": {"dateTime": end_dt.astimezone().isoformat()},
    }


def _create_event_sync(args: dict, enabled: bool, creds: Credentials | None, calendar_id: str | None) -> dict:
    title   = (args.get("title") or "").strip()
    start_s = (args.get("start") or "").strip()
    end_s   = (args.get("end") or "").strip()
    all_day = bool(args.get("all_day", False))

    if not enabled:
        return ExecutionResult(
            status="app_disabled", message="Calendar actions are turned off.",
            title=title, start=start_s or None, end=end_s or None, all_day=all_day,
        ).to_dict()
    if creds is None:
        return ExecutionResult(
            status="needs_connection", message="Google Calendar isn't connected.",
            title=title, start=start_s or None, end=end_s or None, all_day=all_day,
        ).to_dict()

    start_dt = _parse_dt(start_s)
    if not title or start_dt is None:
        return ExecutionResult(
            status="error", message="Couldn't create the event — missing or invalid title/start.",
            title=title, start=start_s or None, end=end_s or None, all_day=all_day,
        ).to_dict()

    end_dt = _parse_dt(end_s) if end_s else None
    if end_dt is None:
        end_dt = (
            start_dt.replace(hour=0, minute=0, second=0) + timedelta(days=1)
            if all_day else start_dt + _DEFAULT_DURATION
        )
    if all_day:
        start_dt = start_dt.replace(hour=0, minute=0, second=0)

    result_kwargs = dict(title=title, start=start_dt.isoformat(), end=end_dt.isoformat(), all_day=all_day)
    # TARGETING: "primary" is Google's own alias for the user's default
    # calendar — this is the write-targeting fix itself; events land on
    # a real Google calendar instead of whatever iCloud calendar
    # osascript's `first calendar whose writable is true` happened to
    # pick. A per-user default-target-calendar preference is a follow-on.
    target_cal = calendar_id or "primary"
    body = {"summary": title, **_event_body(start_dt, end_dt, all_day)}

    try:
        service = _build_service("calendar", "v3", credentials=creds)
        service.events().insert(calendarId=target_cal, body=body).execute()
        log.info("[google-write] created event %r %s -> %s on calendar=%r",
                  title, start_dt.isoformat(), end_dt.isoformat(), target_cal)
        return ExecutionResult(status="success", message="Event created.", **result_kwargs).to_dict()
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during create: %s", exc)
        return ExecutionResult(
            status="needs_connection", message="Google Calendar isn't connected.", **result_kwargs,
        ).to_dict()
    except Exception as exc:
        log.error("[google-write] create crashed for %r: %s", title, exc)
        return ExecutionResult(status="error", message="Couldn't create the event.", **result_kwargs).to_dict()


async def create_event(args: dict, enabled: bool, creds: Credentials | None, calendar_id: str | None = None) -> dict:
    """Create a calendar event — same args/enabled contract as calendar_
    executor.create_calendar_event, plus `creds` (this user's built
    credentials) and `calendar_id` (defaults to "primary" — see
    _create_event_sync). Never raises; returns the identical {status,
    message, title, start, end, all_day} shape (via ExecutionResult,
    reused directly)."""
    return await _run(lambda: _create_event_sync(args, enabled, creds, calendar_id))


async def move_by_uid(
    uid: str, calendar: str, new_start: str, new_end: str | None, enabled: bool, creds: Credentials | None,
) -> dict:
    """The guarded single-event move — app_disabled/needs_connection gate,
    recurrence refusal, re-verify, duration-preserving new_end
    computation, the write, and a SEPARATE confirm read-back (never
    trusting patch's own echoed response) — same guard order as
    calendar_executor.move_by_uid, same result shape (_move_result,
    reused directly). Never raises."""
    if not enabled:
        return _move_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _move_result("needs_connection", "Google Calendar isn't connected.")

    new_start_dt = _parse_dt((new_start or "").strip())
    if new_start_dt is None:
        return _move_result("error", "Couldn't tell what time to move it to.")
    new_end_dt = _parse_dt((new_end or "").strip()) if new_end else None

    try:
        status, ev = await _get_event(creds, calendar, uid)
        if status == "needs_connection":
            return _move_result("needs_connection", "Google Calendar isn't connected.")
        if status == "not_found":
            return _move_result("not_found", "couldn't find that event")
        if status != "ok":
            return _move_result("error", "Couldn't check that event.")

        title = ev.get("summary") or ""
        orig_start_val, is_all_day = _parsed_start_end(ev.get("start", {}))

        # Fail-safe, same spirit as the osascript executor's recurrence
        # guard: refuse rather than risk silently mutating a whole series.
        if _is_recurring(ev):
            log.info("[google-write] refusing recurring move for uid=%s title=%r", uid, title)
            return _move_result(
                "recurring_unsupported", _RECURRING_MOVE_MSG, title=title, start=orig_start_val,
            )

        # RE-VERIFY immediately before writing — closes the gap between
        # the GET above and the write below (staleness guard).
        status2, ev2 = await _get_event(creds, calendar, uid)
        if status2 == "not_found":
            return _move_result("not_found", "that event's no longer there", title=title, start=orig_start_val)
        if status2 != "ok":
            return _move_result("error", "Couldn't verify that event.", title=title)

        if new_end_dt is None:
            orig_start_dt = _parse_dt(orig_start_val)
            orig_end_val, _ = _parsed_start_end(ev.get("end", {}))
            orig_end_dt = _parse_dt(orig_end_val)
            if orig_start_dt is None or orig_end_dt is None:
                return _move_result("error", "Couldn't read that event's current time.", title=title)
            new_end_dt = new_start_dt + (orig_end_dt - orig_start_dt)

        body = _event_body(new_start_dt, new_end_dt, is_all_day)
        patch_status, _patched = await _patch_event(creds, calendar, uid, body)
        if patch_status == "not_found":
            return _move_result("not_found", "that event's no longer there", title=title, start=orig_start_val)
        if patch_status != "ok":
            return _move_result("error", "couldn't move it", title=title, start=orig_start_val)

        # Confirm read-back — a SEPARATE get, never trusting patch's own
        # echoed response (same discipline as calendar_executor.move_by_uid).
        confirm_status, confirm_ev = await _get_event(creds, calendar, uid)
        if confirm_status != "ok":
            return _move_result("error", "Couldn't confirm the move.", title=title)
        confirmed_start_val, _ = _parsed_start_end(confirm_ev.get("start", {}))
        confirmed_end_val, _ = _parsed_start_end(confirm_ev.get("end", {}))
        if _parse_dt(confirmed_start_val) != new_start_dt or _parse_dt(confirmed_end_val) != new_end_dt:
            log.warning(
                "[google-write] move did not confirm for uid=%s (intended %s-%s, actual %s-%s) — "
                "reporting error, not success", uid, new_start_dt, new_end_dt, confirmed_start_val, confirmed_end_val,
            )
            return _move_result("error", "couldn't confirm the move", title=title)

        log.info("[google-write] moved event %r (uid=%s) to %s -> %s",
                  title, uid, new_start_dt.isoformat(), new_end_dt.isoformat())
        return _move_result(
            "success", title=title, start=new_start_dt.isoformat(), end=new_end_dt.isoformat(), all_day=is_all_day,
        )

    except Exception as exc:
        log.error("[google-write] executor error moving event uid=%s: %s", uid, exc)
        return _move_result("error", "Couldn't move that event.")


async def move_event(args: dict, enabled: bool, creds: Credentials | None) -> dict:
    """Move one specific event, by uid+calendar (skips candidate search)
    or by title fragment — same target-resolution contract as calendar_
    executor.move_event, reusing find_candidates verbatim. Never raises;
    same result shape (_move_result)."""
    if not enabled:
        return _move_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _move_result("needs_connection", "Google Calendar isn't connected.")

    new_start_s = (args.get("new_start") or "").strip()
    if _parse_dt(new_start_s) is None:
        return _move_result("error", "Couldn't tell what time to move it to.")
    new_end_s = (args.get("new_end") or "").strip() or None
    if new_end_s and _parse_dt(new_end_s) is None:
        return _move_result("error", "Couldn't tell what time to move it to.")

    uid            = (args.get("uid") or "").strip()
    calendar       = (args.get("calendar") or "").strip()
    title_fragment = (args.get("title") or "").strip()

    if uid and calendar:
        return await move_by_uid(uid, calendar, new_start_s, new_end_s, enabled, creds)

    if not title_fragment:
        return _move_result("error", "Couldn't tell which event to move.")

    q_status, tier, candidates = await find_candidates(title_fragment, args.get("date"), creds)
    if q_status == "needs_connection":
        return _move_result("needs_connection", "Google Calendar isn't connected.")
    if q_status != "ok":
        return _move_result("error", "Couldn't search the calendar.")

    if tier == "none":
        return _move_result("not_found", "couldn't find that event")

    if tier == "exact" and len(candidates) == 1:
        return await move_by_uid(
            candidates[0]["uid"], candidates[0]["calendar"], new_start_s, new_end_s, enabled, creds,
        )

    return _move_result(
        "disambiguate",
        approximate=(tier == "approx"),
        candidates=[
            {"uid": c["uid"], "title": c.get("title"), "start": c.get("start"),
             "end": c.get("end"), "calendar": c.get("calendar"), "all_day": c.get("all_day", False)}
            for c in candidates
        ],
        new_start=new_start_s,
        new_end=new_end_s,
    )


async def delete_by_uid(uid: str, calendar: str, enabled: bool, creds: Credentials | None) -> dict:
    """The guarded single-event delete — app_disabled/needs_connection
    gate, recurrence refusal, re-verify, delete, and SNAG #4's
    events.list-based confirmation (never a GET-by-id/recount — see
    _confirm_gone_sync) — same guard order as calendar_executor.
    delete_by_uid, same result shape (_delete_result, reused directly).
    Never raises."""
    if not enabled:
        return _delete_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _delete_result("needs_connection", "Google Calendar isn't connected.")

    try:
        status, ev = await _get_event(creds, calendar, uid)
        if status == "needs_connection":
            return _delete_result("needs_connection", "Google Calendar isn't connected.")
        if status == "not_found":
            return _delete_result("not_found", "couldn't find that event")
        if status != "ok":
            return _delete_result("error", "Couldn't check that event.")

        title = ev.get("summary") or ""
        start_val, _ = _parsed_start_end(ev.get("start", {}))

        if _is_recurring(ev):
            log.info("[google-write] refusing recurring delete for uid=%s title=%r", uid, title)
            return _delete_result("recurring_unsupported", _RECURRING_DELETE_MSG, title=title, start=start_val)

        # RE-VERIFY immediately before deleting (staleness guard).
        status2, _ev2 = await _get_event(creds, calendar, uid)
        if status2 == "not_found":
            return _delete_result("not_found", "that event's no longer there", title=title, start=start_val)
        if status2 != "ok":
            return _delete_result("error", "Couldn't verify that event.", title=title)

        end_val, _ = _parsed_start_end(ev.get("end", {}))
        start_dt = _parse_dt(start_val) or datetime.now()
        end_dt = _parse_dt(end_val) or start_dt

        del_status = await _delete_event_raw(creds, calendar, uid)
        if del_status == "not_found":
            return _delete_result("not_found", "that event's no longer there", title=title, start=start_val)
        if del_status != "ok":
            return _delete_result("error", "couldn't remove it", title=title, start=start_val)

        # SNAG #4: confirm via events.list (list-exclusion), NOT a
        # GET-by-id — a GET right after this can still return 200 with
        # status:"cancelled" for a while, which is NOT "still there".
        gone = await _run(lambda: _confirm_gone_sync(creds, calendar, uid, start_dt, end_dt))
        if not gone:
            log.warning(
                "[google-write] delete did not confirm removal for uid=%s — reporting error, not success", uid,
            )
            return _delete_result("error", "couldn't remove it", title=title, start=start_val)

        log.info("[google-write] deleted event %r (uid=%s) from calendar=%r", title, uid, calendar)
        return _delete_result("success", title=title, start=start_val)

    except Exception as exc:
        log.error("[google-write] executor error deleting event uid=%s: %s", uid, exc)
        return _delete_result("error", "Couldn't delete that event.")


async def delete_event(args: dict, enabled: bool, creds: Credentials | None) -> dict:
    """Delete one specific event, by uid+calendar (skips candidate
    search) or by title fragment — same target-resolution contract as
    calendar_executor.delete_event, reusing find_candidates verbatim.
    Never raises; same result shape (_delete_result)."""
    if not enabled:
        return _delete_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _delete_result("needs_connection", "Google Calendar isn't connected.")

    uid            = (args.get("uid") or "").strip()
    calendar       = (args.get("calendar") or "").strip()
    title_fragment = (args.get("title") or "").strip()

    if uid and calendar:
        return await delete_by_uid(uid, calendar, enabled, creds)

    if not title_fragment:
        return _delete_result("error", "Couldn't tell which event to delete.")

    q_status, tier, candidates = await find_candidates(title_fragment, args.get("date"), creds)
    if q_status == "needs_connection":
        return _delete_result("needs_connection", "Google Calendar isn't connected.")
    if q_status != "ok":
        return _delete_result("error", "Couldn't search the calendar.")

    if tier == "none":
        return _delete_result("not_found", "couldn't find that event")

    if tier == "exact" and len(candidates) == 1:
        return await delete_by_uid(candidates[0]["uid"], candidates[0]["calendar"], enabled, creds)

    return _delete_result(
        "disambiguate",
        approximate=(tier == "approx"),
        candidates=[
            {"uid": c["uid"], "title": c.get("title"), "start": c.get("start"),
             "end": c.get("end"), "calendar": c.get("calendar"), "all_day": c.get("all_day", False)}
            for c in candidates
        ],
    )
