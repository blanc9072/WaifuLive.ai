"""
EventKit-backed calendar READ path (EK1) — replaces osascript's per-calendar
`whose start date` scan for the three read consumers that must see
recurring occurrences: the display read (list_events), the scheduler's
busy-set propose step, and add_slots' pre-create re-verify.

Root cause this exists to fix: AppleScript's `every event whose start date
>= X and start date <= Y` tests a recurring event's MASTER start date and
never expands individual occurrences — a benchmark class schedule (9 weekly
recurring occurrences landing on one Monday) came back 0/9 through that
path. EventKit's predicateForEventsWithStartDate_endDate_calendars_ expands
recurrence into per-occurrence instances, each at its own real time —
confirmed 9/9 in the probe spike, in single-digit milliseconds.

WRITES (create/move/delete) and the delete/move CANDIDATE SEARCH
(_find_candidates) stay on osascript, completely untouched — see
core/calendar_executor.py. This module is READ ONLY, and deliberately does
not import Supabase/db, mirroring calendar_executor's own boundary (kept
model-agnostic and reusable, e.g. by a future voice relay).

Permission: EventKit's Calendars access is a SEPARATE TCC bucket from
osascript's Automation permission. Probe finding: once macOS has recorded a
determination — even a wrong one like "Add Only" — requestFullAccessTo
EventsWithCompletion_ will NOT show a dialog again; the only fix from then
on is a manual System Settings change. So a permission miss here is
surfaced as an explicit, distinct status ("needs_permission"), never a
silent empty list — the exact bug class this whole migration exists to
kill: a permission problem must never masquerade as "nothing on your
calendar".

`uid` on returned event dicts is EventKit's eventIdentifier (shared across
an entire recurring series, confirmed in the probe) — display only. It is
never round-tripped into delete_by_uid/move_by_uid: those resolve against
osascript's OWN uid space via calendar_executor._find_candidates, a
completely separate namespace. Confirmed by inspection that the client only
ever feeds a delete/move candidate's uid (itself sourced from
_find_candidates) back into those endpoints — never a uid from a list read.
"""

import asyncio
import logging
from datetime import datetime

import EventKit

log = logging.getLogger(__name__)

_store: EventKit.EKEventStore | None = None

_STATUS_NAMES = {
    0: "not_determined",
    1: "restricted",
    2: "denied",
    3: "full",
    4: "write_only",
}


def _get_store() -> EventKit.EKEventStore:
    global _store
    if _store is None:
        _store = EventKit.EKEventStore.alloc().init()
    return _store


async def _run(fn):
    return await asyncio.to_thread(fn)


def _permission_status_sync() -> str:
    try:
        status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
    except Exception as exc:
        log.error("[ek-read] authorizationStatusForEntityType_ crashed: %s", exc)
        return "denied"
    return _STATUS_NAMES.get(status, "denied")


def _request_access_sync() -> None:
    """Fire-and-forget: only shows a real OS dialog on a genuinely fresh
    (not_determined) install — a prior determination of any kind (even
    write_only) is never re-prompted, per the probe. Callers re-check
    _permission_status_sync() on their OWN next call; this never blocks
    waiting on the completion callback."""
    try:
        store = _get_store()
        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(lambda granted, error: None)
        else:
            store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, lambda granted, error: None)
    except Exception as exc:
        log.error("[ek-read] request_access crashed: %s", exc)


async def permission_status() -> str:
    """"full" | "denied" | "not_determined" | "restricted" | "write_only" —
    never raises. Only "full" makes read_window/enumerate_calendars
    trustworthy; every other value is what read_window's needs_permission
    status is reporting."""
    return await _run(_permission_status_sync)


def _enumerate_calendars_sync() -> list[dict]:
    try:
        store = _get_store()
        cals = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
        return [{"title": c.title(), "writable": bool(c.allowsContentModifications())} for c in cals]
    except Exception as exc:
        log.error("[ek-read] enumerate_calendars crashed: %s", exc)
        return []


async def enumerate_calendars() -> list[dict]:
    """[{"title": str, "writable": bool}, ...] for every EventKit event
    calendar — never raises, [] on any failure including no permission.
    Callers that need to distinguish "no permission" from "genuinely no
    calendars" should check permission_status() first, same contract as
    read_window."""
    return await _run(_enumerate_calendars_sync)


def _to_local_naive_iso(ns_date) -> str | None:
    if ns_date is None:
        return None
    try:
        return datetime.fromtimestamp(ns_date.timeIntervalSince1970()).isoformat(timespec="seconds")
    except Exception:
        return None


def _read_window_sync(start_dt: datetime, end_dt: datetime, writable_only: bool) -> dict:
    status = _permission_status_sync()
    if status != "full":
        _request_access_sync()
        log.info("[ek-read] permission not full (status=%s) — returning needs_permission", status)
        return {"status": "needs_permission", "events": []}

    try:
        store = _get_store()
        all_cals = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
        cals = [c for c in all_cals if not writable_only or c.allowsContentModifications()]
        if not cals:
            # writable_only=True with genuinely no writable calendars is a
            # real, correct empty — not a failure to distinguish from one.
            return {"status": "ok", "events": []}

        start = EventKit.NSDate.dateWithTimeIntervalSince1970_(start_dt.timestamp())
        end = EventKit.NSDate.dateWithTimeIntervalSince1970_(end_dt.timestamp())
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, cals)
        ek_events = store.eventsMatchingPredicate_(predicate)

        events = [
            {
                "uid": ev.eventIdentifier(),
                "title": ev.title(),
                "start": _to_local_naive_iso(ev.startDate()),
                "end": _to_local_naive_iso(ev.endDate()),
                "all_day": bool(ev.isAllDay()),
                "calendar": ev.calendar().title(),
            }
            for ev in ek_events
        ]
        return {"status": "ok", "events": events}
    except Exception as exc:
        log.error("[ek-read] read_window crashed: %s", exc)
        return {"status": "error", "events": []}


async def read_window(start_dt: datetime, end_dt: datetime, writable_only: bool) -> dict:
    """THE replacement read: predicateForEventsWithStartDate_endDate_calendars_
    expands recurring events into per-occurrence instances (the entire
    point — see module docstring). Never raises. Returns one of:

      {"status": "needs_permission", "events": []}
        EventKit access is not fullAccess. NEVER an empty "ok" — a
        permission miss must never masquerade as "nothing on your
        calendar". Also fires a (harmless if already-determined) access
        request as a side effect, so a genuinely fresh install gets its
        one real OS prompt.

      {"status": "ok", "events": [{uid, title, start, end, all_day,
       calendar}, ...]}
        start/end are LOCAL-naive ISO strings at the OCCURRENCE's actual
        time (never the recurring master's) — the same convention every
        other event dict in this codebase already uses.

      {"status": "error", "events": []}
        An unexpected failure querying EventKit itself.

    writable_only=True filters to allowsContentModifications calendars,
    matching calendar_executor.list_events_window's own writable_only
    contract exactly — the scheduler's busy-set and add_slots' re-verify
    both need this so a read-only holiday/subscription never blocks a
    slot. writable_only=False (the display read) includes every calendar,
    read-only ones included — Siri Suggestions and Scheduled Reminders
    simply never appear under EventKit's event entity type at all (Siri
    Suggestions isn't a real EKCalendar; Scheduled Reminders is a
    Reminders-type calendar, a different EventKit entity), so the old
    osascript-side exclusion logic for Siri Suggestions is unnecessary
    here rather than something to port.
    """
    return await _run(lambda: _read_window_sync(start_dt, end_dt, writable_only))
