"""
Stage 3 — executes resolved calendar actions (create_event, list_events,
delete_event) against macOS Calendar via AppleScript (osascript).

Standalone and model-agnostic: takes a plain args dict, returns a plain dict.
Deliberately does NOT import routes, request/response models, Supabase, or
anything voice/Live-specific, so the voice relay can reuse these functions
unchanged once that path is wired up.

Gating (checked in order, default-deny):
  1. `enabled` — the caller's already-resolved permission decision. This
     module has no opinion on WHERE that comes from (profiles.calendar_enabled
     via core.db, a future org-level policy, whatever) — it just refuses to
     run when told no, so every caller must explicitly decide rather than
     inheriting a silent default-allow.
  2. macOS Calendar automation permission, enforced by the OS.

The permission-denied signature below was captured live on this machine by
revoking Calendar automation for the requesting app (VS Code) and rerunning
osascript: exit code 1, stderr
  "execution error: Not authorized to send Apple events to Calendar. (-1743)"
Matched loosely (substring + code) since exact wording can vary by macOS version.

list_events() is built on the query verified in the /tmp/cal_list_spike.py
read-spike: field-by-field date range (no locale string parsing), a
calendar-names-first-then-reenter-by-name loop (works around an AppleScript
-1728 "Can't get item 1 of ..." error that chaining a second `whose` off a
filtered calendar reference triggers), and a hand-rolled JSON escaper so
Python's json.loads() can parse the result reliably.

delete_event() is built on /tmp/cal_delete_spike.py, which found that
AppleScript's `delete` on a RECURRING event's master reports success while
silently changing nothing — no error, no partial effect. Every gate in
delete_event (candidate resolution, recurrence refusal, re-verify, and a
post-delete recount) exists specifically to make that failure mode
unreachable: see the comment above delete_event() for the full rationale.

move_event() is built on /tmp/cal_move_spike.py, which found the OPPOSITE
failure mode from delete on a recurring master: the write PERSISTS, and
silently shifts the WHOLE recurring series (base time changes, recurrence
rule untouched) rather than one occurrence — so move refuses recurring
events too, but to prevent a quiet full-series mutation, not a no-op. The
spike also found Calendar.app validates start<end against whatever is
CURRENTLY set the instant a property is assigned — unconditionally writing
`start date` before `end date` fails loudly (-10025) whenever the new start
is later than the event's not-yet-updated end — so the write always
compares the new start to the live end date and orders the two assignments
to avoid ever creating a transient invalid interval. See the comment above
move_event() for the full rationale.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_TIMEOUT = 10.0
_PER_CALENDAR_TIMEOUT = 40.0   # every multi-calendar read (list_events,
                                # list_events_window, _find_candidates) queries
                                # each calendar as its OWN osascript subprocess,
                                # concurrently — this is the budget for ONE
                                # calendar, not the whole scan.
                                #
                                # IMPORTANT, measured directly: Calendar.app
                                # does NOT actually service N concurrent
                                # AppleEvent requests in parallel — launching
                                # 9 separate osascript processes (one per
                                # calendar) at once still took ~36s for ALL of
                                # them to clear, confirmed at the raw
                                # subprocess level with no Python/asyncio
                                # involved, and unaffected by limiting how many
                                # ran at once (batches of 3 also took ~36s
                                # total). Concurrency's real, load-bearing
                                # benefit here is ISOLATION, not raw speed:
                                # one slow/hanging calendar can no longer sink
                                # every other calendar's already-arrived data,
                                # because each one gets its OWN independent
                                # timeout instead of sharing one budget with
                                # every other calendar (the old design's bug —
                                # two slow calendars alone could exceed even a
                                # 50s shared budget and return a bare empty).
                                # 20s was tried first and cut real reads off
                                # mid-flight, marking calendars that would
                                # have answered within ~30-36s as falsely
                                # "unreachable" — 40s gives real, responding-
                                # but-slow calendars enough room to actually
                                # finish, while still bounding worst-case
                                # wall-clock far below the old design's
                                # unbounded-by-comparison sum (measured up to
                                # 84s, with total failure past that). There is
                                # deliberately no separate "whole scan"
                                # timeout anymore.
_DEFAULT_DURATION = timedelta(hours=1)
_OS_DENIED_MARKERS = ("not authorized to send apple events", "-1743")


@dataclass
class ExecutionResult:
    status:  str            # "success" | "app_disabled" | "os_denied" | "error"
    message: str
    title:   str | None = None
    start:   str | None = None
    end:     str | None = None
    all_day: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_dt(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _applescript_date_setter(var: str, dt: datetime) -> str:
    # Sets fields on `current date` numerically rather than parsing a date
    # STRING — AppleScript's string->date parsing is locale-dependent and
    # broke across machines in testing; field assignment is not.
    return (
        f"set {var} to (current date)\n"
        f"set year of {var} to {dt.year}\n"
        f"set month of {var} to {dt.month}\n"
        f"set day of {var} to {dt.day}\n"
        f"set hours of {var} to {dt.hour}\n"
        f"set minutes of {var} to {dt.minute}\n"
        f"set seconds of {var} to {dt.second}\n"
    )


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def create_calendar_event(args: dict, enabled: bool) -> dict:
    """Create a calendar event from a resolved create_event args dict.

    `enabled` is the caller's already-resolved permission decision — no
    default, so a caller can't accidentally omit it and get a silent allow.
    e.g. /chat passes core.db.fetch_calendar_enabled(user_id) here.

    Never raises — every failure path returns a status dict. Synchronous
    callers (e.g. /chat) can await this without risking the turn; on
    timeout or any executor failure this returns status="error".
    """
    title   = (args.get("title") or "").strip()
    start_s = (args.get("start") or "").strip()
    end_s   = (args.get("end") or "").strip()
    all_day = bool(args.get("all_day", False))

    if not enabled:
        return ExecutionResult(
            status="app_disabled",
            message="Calendar actions are turned off.",
            title=title, start=start_s or None, end=end_s or None, all_day=all_day,
        ).to_dict()

    start_dt = _parse_dt(start_s)
    if not title or start_dt is None:
        return ExecutionResult(
            status="error",
            message="Couldn't create the event — missing or invalid title/start.",
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

    script = (
        'tell application "Calendar"\n'
        '  set targetCal to first calendar whose writable is true\n'
        '  tell targetCal\n'
        f'    {_applescript_date_setter("startDate", start_dt)}'
        f'    {_applescript_date_setter("endDate", end_dt)}'
        f'    make new event with properties {{summary:"{_escape(title)}", '
        f'start date:startDate, end date:endDate, allday event:{"true" if all_day else "false"}}}\n'
        '  end tell\n'
        'end tell\n'
    )

    result_kwargs = dict(
        title=title, start=start_dt.isoformat(), end=end_dt.isoformat(), all_day=all_day,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("[stage3] osascript timed out creating %r", title)
            return ExecutionResult(
                status="error", message="Calendar didn't respond in time.", **result_kwargs,
            ).to_dict()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            low = err.lower()
            if any(marker in low for marker in _OS_DENIED_MARKERS):
                log.info("[stage3] os permission denied: %s", err.strip())
                return ExecutionResult(
                    status="os_denied",
                    message="This app doesn't have permission to access Calendar on macOS.",
                    **result_kwargs,
                ).to_dict()
            log.warning("[stage3] osascript failed (%d): %s", proc.returncode, err.strip())
            return ExecutionResult(
                status="error", message="Couldn't create the event.", **result_kwargs,
            ).to_dict()

        log.info("[stage3] created event %r %s -> %s", title, start_dt.isoformat(), end_dt.isoformat())
        return ExecutionResult(
            status="success", message="Event created.", **result_kwargs,
        ).to_dict()

    except Exception as exc:
        log.error("[stage3] executor error for %r: %s", title, exc)
        return ExecutionResult(
            status="error", message="Couldn't create the event.", **result_kwargs,
        ).to_dict()


# ---------------------------------------------------------------------------
# list_events — reads a day's events. Returns a plain dict (not
# ExecutionResult — the shape doesn't fit: a list result carries a `date`
# and a list of `events`, not a single title/start/end/all_day).
# ---------------------------------------------------------------------------

# Static AppleScript handlers, copied verbatim from the verified read-spike
# (/tmp/cal_list_spike.py): zero-padded field formatting for ISO strings,
# and a hand-rolled JSON-string escaper (\, ", embedded newlines) so
# Python's json.loads() can parse the result reliably.
_LIST_APPLESCRIPT_HELPERS = r'''
on pad2(n)
    if n < 10 then
        return "0" & (n as string)
    else
        return (n as string)
    end if
end pad2

on isoDate(d)
    set y to (year of d) as string
    set mo to my pad2((month of d) as integer)
    set da to my pad2(day of d)
    set hh to my pad2(hours of d)
    set mi to my pad2(minutes of d)
    set se to my pad2(seconds of d)
    return y & "-" & mo & "-" & da & "T" & hh & ":" & mi & ":" & se
end isoDate

on replaceText(theText, oldStr, newStr)
    set savedDelims to AppleScript's text item delimiters
    set AppleScript's text item delimiters to oldStr
    set theItems to text items of theText
    set AppleScript's text item delimiters to newStr
    set theText to theItems as string
    set AppleScript's text item delimiters to savedDelims
    return theText
end replaceText

on jsonEscape(s)
    set s to my replaceText(s, "\\", "\\\\")
    set s to my replaceText(s, "\"", "\\\"")
    set s to my replaceText(s, return, " ")
    set s to my replaceText(s, linefeed, " ")
    return s
end jsonEscape
'''

# __START_RANGE_SETTER__ / __END_RANGE_SETTER__ are filled in by
# _applescript_date_setter() below, field-by-field (no locale string
# parsing) — same technique create_calendar_event() uses for startDate/
# endDate. __CALENDAR_FILTER__ is filled in wherever this appears —
# `whose writable is true` (candidates/busy-set queries, the default) or
# nothing at all (the display read, which wants every calendar including
# read-only ones like a Google "Holidays" calendar).
#
# CONCURRENT, PER-CALENDAR READS. Every multi-calendar read used to be ONE
# AppleScript `repeat` looping every calendar SEQUENTIALLY inside a single
# osascript process, bounded by one shared timeout for the whole scan —
# so two slow external/subscription calendars summed their latency into
# that one budget (measured: a single "today" read took 83.8s against a
# 50s timeout). The fix: enumerate calendar NAMES first (fast — plain
# metadata, sub-second even across 9 calendars), then query EACH
# calendar's events as its OWN osascript subprocess with its OWN timeout
# (_PER_CALENDAR_TIMEOUT), launched CONCURRENTLY via asyncio.gather.
# Wall-clock becomes ~max(one calendar's latency), not the sum.
#
# LEGIBLE RESULT is the load-bearing invariant this restructure exists
# for: a calendar that times out or errors drops ITSELF (recorded by name
# in `unreachable`) rather than silently producing an empty list that's
# indistinguishable from a genuinely empty calendar — see
# _read_calendars_concurrent for the exact success/partial/error rules.
#
# Calendar names are re-entered BY NAME (`tell calendar calName`) before
# the event lookup — the -1728 fix from the read-spike. Chaining a second
# `whose` off a calendar reference pulled from an earlier `whose`-filtered
# list raised "Can't get item 1 of ..." there; never reuse a filtered
# calendar reference for the inner query.
#
# Quirk: Calendar stores an all-day event's `end date` as 23:59:59 the SAME
# day, not midnight the next day. `all_day` is authoritative — callers must
# not do exclusive-next-day math on `end` for all-day events.
_CALENDAR_NAMES_TEMPLATE = r'''
tell application "Calendar"
    set calNames to name of (every calendar __CALENDAR_FILTER__)
end tell
set jsonParts to {}
repeat with n in calNames
    set end of jsonParts to "\"" & my jsonEscape(n) & "\""
end repeat
set AppleScript's text item delimiters to ","
set jsonArray to "[" & (jsonParts as string) & "]"
set AppleScript's text item delimiters to ""
return jsonArray
'''

_SINGLE_CALENDAR_QUERY_TEMPLATE = r'''
__START_RANGE_SETTER__
__END_RANGE_SETTER__
set jsonParts to {}

tell application "Calendar"
    tell calendar "__CAL_NAME__"
        set theEvents to (every event whose start date ≥ startRange and start date ≤ endRange)
        repeat with evt in theEvents
            set evtUID to uid of evt
            set evtTitle to summary of evt
            set evtStart to start date of evt
            set evtEnd to end date of evt
            set evtAllDay to allday event of evt
            set jsonObj to "{\"uid\":\"" & my jsonEscape(evtUID) & "\",\"title\":\"" & my jsonEscape(evtTitle) & "\",\"start\":\"" & my isoDate(evtStart) & "\",\"end\":\"" & my isoDate(evtEnd) & "\",\"all_day\":" & (evtAllDay as string) & "}"
            set end of jsonParts to jsonObj
        end repeat
    end tell
end tell

set AppleScript's text item delimiters to ","
set jsonArray to "[" & (jsonParts as string) & "]"
set AppleScript's text item delimiters to ""
return jsonArray
'''


async def _enumerate_calendar_names(writable_only: bool) -> tuple[str, list[str]]:
    """Fast metadata-only osascript call — fetching calendar NAMES is cheap
    regardless of external sync state (measured sub-second across 9
    calendars, including the slow subscription ones); the SLOW part is
    querying each calendar's EVENTS, which is why that's a separate,
    per-calendar, concurrent step (_query_one_calendar /
    _read_calendars_concurrent). Returns (status, names): status is
    "ok" | "os_denied" | "error" | "timeout"."""
    calendar_filter = "whose writable is true" if writable_only else ""
    script = _LIST_APPLESCRIPT_HELPERS + _CALENDAR_NAMES_TEMPLATE.replace(
        "__CALENDAR_FILTER__", calendar_filter
    )
    code, out, err, timed_out = await _run_osa(script, _TIMEOUT)
    if timed_out:
        return "timeout", []
    if code != 0:
        return ("os_denied" if _is_os_denied(err) else "error"), []
    try:
        return "ok", json.loads(out.strip())
    except (json.JSONDecodeError, ValueError):
        return "error", []


async def _query_one_calendar(
    cal_name: str, start_dt: datetime, end_dt: datetime, timeout: float,
) -> tuple[str, list]:
    """One calendar, one osascript subprocess, one independent timeout.
    Returns (status, events): status is "ok" | "os_denied" | "error" |
    "timeout". `calendar` is attached to each event dict in PYTHON from
    the already-known `cal_name` afterward — the script itself no longer
    needs to echo the name back through a second layer of AppleScript
    string escaping for something the caller already knows."""
    script = _LIST_APPLESCRIPT_HELPERS + (
        _SINGLE_CALENDAR_QUERY_TEMPLATE
        .replace("__START_RANGE_SETTER__", _applescript_date_setter("startRange", start_dt).rstrip("\n"))
        .replace("__END_RANGE_SETTER__", _applescript_date_setter("endRange", end_dt).rstrip("\n"))
        .replace("__CAL_NAME__", _escape(cal_name))
    )
    code, out, err, timed_out = await _run_osa(script, timeout)
    if timed_out:
        return "timeout", []
    if code != 0:
        return ("os_denied" if _is_os_denied(err) else "error"), []
    try:
        events = json.loads(out.strip())
    except (json.JSONDecodeError, ValueError):
        return "error", []
    for ev in events:
        ev["calendar"] = cal_name
    return "ok", events


async def _read_calendars_concurrent(
    start_dt: datetime, end_dt: datetime, writable_only: bool,
    per_calendar_timeout: float = _PER_CALENDAR_TIMEOUT,
) -> dict:
    """THE shared concurrent read used by list_events, list_events_window,
    and _find_candidates — see the module comment above
    _CALENDAR_NAMES_TEMPLATE for the full rationale. Returns:

      {"status": "success", "events": [...], "unreachable": [...]}
        At least one calendar responded. `unreachable` lists the NAMES of
        any calendar that timed out or errored — empty only when EVERY
        calendar responded, which is the one case that means genuinely
        empty rather than partially unknown.

      {"status": "os_denied", "events": [], "unreachable": []}
        Calendar automation permission is off. This is a whole-app
        condition (the same OS permission gates every calendar equally),
        so it always short-circuits the aggregate result rather than
        being folded into `unreachable` alongside ordinary per-calendar
        flakiness.

      {"status": "error", "events": [], "unreachable": [...]}
        Name enumeration itself failed, OR every single calendar that was
        queried also failed/timed out. NEVER silently reported as an
        empty success — that silent collapse is exactly the bug this
        restructure exists to close.
    """
    enum_status, cal_names = await _enumerate_calendar_names(writable_only)
    if enum_status == "os_denied":
        return {"status": "os_denied", "events": [], "unreachable": []}
    if enum_status != "ok":
        return {"status": "error", "events": [], "unreachable": []}
    if not cal_names:
        # No calendars to query at all (e.g. writable_only=True and the
        # user happens to have none) is a real, correct empty — not a
        # failure to distinguish from one.
        return {"status": "success", "events": [], "unreachable": []}

    results = await asyncio.gather(
        *(_query_one_calendar(name, start_dt, end_dt, per_calendar_timeout) for name in cal_names)
    )

    if any(status == "os_denied" for status, _ in results):
        return {"status": "os_denied", "events": [], "unreachable": []}

    events: list = []
    unreachable: list[str] = []
    for name, (status, cal_events) in zip(cal_names, results):
        if status == "ok":
            events.extend(cal_events)
        else:
            unreachable.append(name)

    if len(unreachable) == len(cal_names):
        # Every single calendar failed — a genuine failure to read
        # anything at all, never surfaced as an empty success.
        return {"status": "error", "events": [], "unreachable": unreachable}

    return {"status": "success", "events": events, "unreachable": unreachable}


def _resolve_list_date(date_str: str | None) -> datetime:
    """Target day at local midnight. Defaults to tomorrow when `date_str`
    is absent or not a parseable ISO date — never raises."""
    if date_str:
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d")
        except ValueError:
            pass
    return (datetime.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _list_result(
    status: str, events: list,
    date_iso: str | None = None,
    window_start: str | None = None, window_end: str | None = None,
    message: str | None = None, unreachable: list | None = None,
) -> dict:
    """Single-day results carry `date`; multi-day RANGE results carry
    `window_start`/`window_end` instead — never both. Presence of
    window_start is how chat.html tells the two shapes apart and picks
    the right renderer (grouped-by-day for a range, one-line for a day)."""
    result: dict = {"status": status, "kind": "list"}
    if message is not None:
        result["message"] = message
    if date_iso is not None:
        result["date"] = date_iso
    if window_start is not None:
        result["window_start"] = window_start
    if window_end is not None:
        result["window_end"] = window_end
    result["events"] = events
    if unreachable:
        result["unreachable"] = unreachable
    return result


async def list_events(args: dict, enabled: bool) -> dict:
    """List events from macOS Calendar — either a single day (default
    tomorrow, the original behavior, unchanged) or a multi-day RANGE when
    the resolver fills BOTH window_start and window_end ("this week",
    "this weekend", "next few days") instead of `date`.

    `enabled` is the caller's already-resolved permission decision — same
    contract as create_calendar_event: no default, so a caller can't
    accidentally omit it and get a silent allow.

    writable_only=False: this is the plain "what's on my calendar" DISPLAY
    read — read-only calendars (a subscribed Google "Holidays" calendar,
    Birthdays, Siri Suggestions, ...) should be visible here, single-day
    or ranged alike. Never change this to True — that would hide real
    events from the user's own view of their day/week. Never copy this
    False into a scheduler or delete/move call site; see
    _read_calendars_concurrent.

    A range read reuses the EXACT SAME concurrent, per-calendar
    _read_calendars_concurrent as the single-day path — that function
    doesn't care how wide [start_dt, end_dt] is, so scanning a week costs
    the same wall-clock as scanning a day (bounded by the single slowest
    calendar, not by how many days are in the window, and not by how
    many events exist in it).

    Never raises — every failure path returns a status dict. A partial
    read (some calendars unreachable, others fine) still reports
    status="success" with an `unreachable` list attached — see
    _read_calendars_concurrent's docstring for why an empty result is
    never returned unless every calendar genuinely was empty.
    """
    window_start_str = (args.get("window_start") or "").strip()
    window_end_str = (args.get("window_end") or "").strip()

    if window_start_str or window_end_str:
        # A RANGE was requested — both bounds are required. Fail closed
        # with a real error rather than silently falling back to a single
        # default day (_resolve_list_date's fallback), which would
        # quietly answer a narrower question than what was actually
        # asked. _parse_dt (not _resolve_list_date) is used deliberately
        # here: it returns None on anything unparseable instead of ever
        # guessing a default.
        start_dt = _parse_dt(window_start_str) if window_start_str else None
        end_dt = _parse_dt(window_end_str) if window_end_str else None
        if start_dt is None or end_dt is None or end_dt < start_dt:
            return _list_result("error", [], message="Couldn't tell what date range to check.")

        window_start_iso = start_dt.strftime("%Y-%m-%d")
        window_end_iso = end_dt.strftime("%Y-%m-%d")

        if not enabled:
            return _list_result(
                "app_disabled", [], window_start=window_start_iso, window_end=window_end_iso,
                message="Calendar actions are turned off.",
            )

        range_start = start_dt
        range_end = end_dt.replace(hour=23, minute=59, second=59)

        read = await _read_calendars_concurrent(range_start, range_end, writable_only=False)

        if read["status"] == "os_denied":
            return _list_result(
                "os_denied", [], window_start=window_start_iso, window_end=window_end_iso,
                message="This app doesn't have permission to access Calendar on macOS.",
            )
        if read["status"] != "success":
            log.warning("[stage3] failed to read any calendar listing events for %s..%s",
                        window_start_iso, window_end_iso)
            return _list_result(
                "error", [], window_start=window_start_iso, window_end=window_end_iso,
                message="Couldn't read the calendar.",
            )

        events = read["events"]
        unreachable = read["unreachable"]
        log.info("[stage3] listed %d event(s) across %s..%s%s", len(events),
                 window_start_iso, window_end_iso,
                 f" (unreachable: {unreachable})" if unreachable else "")
        return _list_result(
            "success", events, window_start=window_start_iso, window_end=window_end_iso,
            unreachable=unreachable,
        )

    # Single-day path — unchanged behavior.
    target = _resolve_list_date(args.get("date"))
    date_iso = target.strftime("%Y-%m-%d")

    if not enabled:
        return _list_result("app_disabled", [], date_iso=date_iso, message="Calendar actions are turned off.")

    start_dt = target
    end_dt = target.replace(hour=23, minute=59, second=59)

    read = await _read_calendars_concurrent(start_dt, end_dt, writable_only=False)

    if read["status"] == "os_denied":
        return _list_result(
            "os_denied", [], date_iso=date_iso,
            message="This app doesn't have permission to access Calendar on macOS.",
        )
    if read["status"] != "success":
        log.warning("[stage3] failed to read any calendar listing events for %s", date_iso)
        return _list_result("error", [], date_iso=date_iso, message="Couldn't read the calendar.")

    events = read["events"]
    unreachable = read["unreachable"]
    log.info("[stage3] listed %d event(s) for %s%s", len(events), date_iso,
             f" (unreachable: {unreachable})" if unreachable else "")
    return _list_result("success", events, date_iso=date_iso, unreachable=unreachable)


# ---------------------------------------------------------------------------
# delete_event — deletes ONE specific event by uid. Delete is irreversible,
# and /tmp/cal_delete_spike.py found a serious silent-failure trap: AppleScript
# `delete` on a RECURRING event's master reports success while changing
# nothing at all — no error, no partial effect, just a no-op dressed as a
# confirmed delete. Every gate below exists because of that spike, in this
# fixed order, and none of them are optional:
#   1. app_disabled if not enabled.
#   2. Resolve candidates (title fragment + date, or an already-known
#      uid+calendar from a prior list_events() result). 0 -> not_found,
#      >1 -> ambiguous (never guess which one), exactly 1 -> continue.
#   3. Recurrence refusal — read the candidate's `recurrence` property BEFORE
#      anything destructive. Non-empty (or unreadable) -> refuse. Fail-safe:
#      an error reading the property is treated as "recurring" too, never as
#      "safe to proceed" — the spike's whole point is that uncertainty here
#      is exactly what produces a false "success".
#   4. Re-verify the uid still exists immediately before deleting (catches a
#      stale target — deleted elsewhere between candidate resolution and now).
#   5. Delete + save + recount, all in one call, and only report success if
#      the post-delete count is confirmed 0. A nonzero remaining count after
#      a reported delete IS the silent-no-op trap — never surface that as
#      success.
# ---------------------------------------------------------------------------

_DELETE_TIMEOUT = 15.0            # single-calendar steps: recurrence check, re-verify, delete+confirm


def _delete_result(
    status: str,
    message: str | None = None,
    title: str | None = None,
    start: str | None = None,
    candidates: list | None = None,
    approximate: bool | None = None,
) -> dict:
    result: dict = {"status": status, "kind": "delete"}
    if message is not None:
        result["message"] = message
    if title is not None:
        result["title"] = title
    if start is not None:
        result["start"] = start
    if candidates is not None:
        result["candidates"] = candidates
    if approximate is not None:
        result["approximate"] = approximate
    return result


async def _run_osa(script: str, timeout: float) -> tuple[int, str, str, bool]:
    """One osascript round trip for the delete pipeline. Returns
    (returncode, stdout, stderr, timed_out) — a timeout is reported via the
    flag rather than an exception, so every call site has one uniform way
    to check both outcomes."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "", True
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace"), False


def _is_os_denied(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _OS_DENIED_MARKERS)


# Candidate resolution is a two-part fix over what shipped first, driven by
# a live-trace investigation (13 real chat->resolver->executor trials) that
# found two distinct bugs:
#
#   (a) NOISE IN THE QUERY — the chat model appended generic nouns the user
#       never said ("gym" -> "gym event") in 12/13 trials. An AppleScript
#       `summary contains "gym event"` against a real title of literally
#       "gym" matches NOTHING, so a real event silently reported not_found.
#       Fixed at the source (prompts.py + tool_resolver.py instructions)
#       AND with a deterministic backstop here — the model won't always
#       comply, so the executor normalizes and strips a trailing generic
#       noun itself rather than trusting the prompt alone.
#
#   (b) SINGLE-DAY SCOPE — the old candidate query only ever searched ONE
#       day (the resolved date, defaulting to tomorrow). Two same-titled
#       events on DIFFERENT days never both surfaced — whichever one fell
#       on the assumed day was found alone and deleted without ever
#       reaching the ambiguous branch. Fixed by widening the search window
#       to today..+7 days whenever no explicit date was given, and by
#       matching in PYTHON (bidirectional normalized substring, not a
#       brittle AppleScript `contains`) against the SAME range query
#       list_events() already proved — over-matching here is safe, since it
#       only ever produces an ambiguous "which one?", never a wrong delete.
_GENERIC_EVENT_NOUNS = frozenset({
    "event", "events", "meeting", "meetings", "appointment", "appointments", "appt", "appts",
})
_GENERIC_EVENT_NOUNS_BY_LEN = tuple(sorted(_GENERIC_EVENT_NOUNS, key=len, reverse=True))


def _normalize_title(s: str) -> str:
    """Lowercase, collapse ALL whitespace out, then strip a single
    TRAILING generic calendar noun as a plain suffix.

    Whitespace is collapsed BEFORE the noun-strip (not after) so a
    misplaced-space typo normalizes identically to the correctly-spaced
    original: "dinne rmeeting" and "dinner meeting" both collapse to
    "dinnermeeting" first (concatenation doesn't care where the space
    was), and only THEN does the "meeting" suffix get stripped from that
    shared string, leaving "dinner" either way. Checking the noun as a
    suffix (not a whitespace-delimited word) is what makes this order-
    independent — a word-boundary check would only fire on the correctly-
    spaced input and the two strings would end up unequal.

    Never strips down to an empty string — "event" alone stays "event"
    rather than becoming "" (which would trivially match everything);
    even that would only ever over-match into an ambiguous/approximate
    result, never a wrong delete, but there's no reason to invite it.
    """
    collapsed = "".join((s or "").strip().lower().split())
    for noun in _GENERIC_EVENT_NOUNS_BY_LEN:
        if collapsed.endswith(noun) and len(collapsed) > len(noun):
            return collapsed[: -len(noun)]
    return collapsed


def _osa_distance(a: str, b: str) -> int:
    """Optimal String Alignment distance: Levenshtein (insertion, deletion,
    substitution) plus adjacent-transposition as a single edit. OSA, not
    full Damerau-Levenshtein — no substring may be edited more than once,
    which is the standard, simpler variant and all that's needed here.
    Implemented directly (no new dependency; the venv is a pinned freeze).
    `a`/`b` are expected to already be normalized via _normalize_title."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(
                d[i - 1][j] + 1,        # deletion
                d[i][j - 1] + 1,        # insertion
                d[i - 1][j - 1] + cost,  # substitution
            )
            if (i > 1 and j > 1
                    and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)   # adjacent transposition
    return d[la][lb]


def _approx_threshold(normalized_query: str) -> int:
    """Length-scaled edit-distance budget for the approximate tier — about
    1 edit per 5 characters, capped at 2. Generosity here is safe: the
    approximate tier ALWAYS requires confirmation before deleting (see
    delete_event), so a looser threshold only ever produces a "did you
    mean?" the caller must accept, never a silent wrong delete."""
    n = len(normalized_query)
    return min(2, max(1, n // 5))


def _titles_match(query_norm: str, event_title_norm: str) -> bool:
    """Bidirectional containment on normalized text: the query might be a
    fragment of the real title ("gym" in "gym class") or the real title
    might be a fragment of what the user said ("standup" in "the standup
    meeting" once normalized down to "the standup")."""
    if not query_norm or not event_title_norm:
        return False
    return query_norm in event_title_norm or event_title_norm in query_norm


def _resolve_delete_window(date_str: str | None) -> tuple[datetime, datetime]:
    """Search window for delete candidates. An explicit, parseable date
    scopes to that single day (unchanged from before). No date — or an
    unparseable one — WIDENS to today..+7 days, so two same-titled events
    on different days both surface instead of only whichever one an
    assumed single day happened to contain."""
    if date_str:
        try:
            day = datetime.strptime(date_str.strip(), "%Y-%m-%d")
            return day, day.replace(hour=23, minute=59, second=59)
        except ValueError:
            pass
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = (today + timedelta(days=7)).replace(hour=23, minute=59, second=59)
    return today, end


def _to_candidate(ev: dict) -> dict:
    return {
        "uid":      ev.get("uid"),
        "title":    ev.get("title"),
        "start":    ev.get("start"),
        "end":      ev.get("end"),
        "calendar": ev.get("calendar"),
        "all_day":  ev.get("all_day", False),
    }


async def _find_candidates(title_fragment: str, date_str: str | None) -> tuple[str, str, list]:
    """Finds delete candidates by reusing the same concurrent, per-calendar
    range read as list_events/list_events_window (_read_calendars_concurrent)
    over the resolved window, then matching titles in Python in TWO TIERS:

      EXACT  — normalized bidirectional containment (_titles_match on
               _normalize_title'd text). Covers the noise/whitespace cases
               (generic-noun suffixes, misplaced spaces, case) — these
               collapse to byte-identical strings, so this tier is fully
               deterministic. Checked first; if anything matches here, the
               approximate tier is never even computed.

      APPROX — only reached when EXACT found nothing. Optimal String
               Alignment distance (_osa_distance) against a length-scaled
               threshold (_approx_threshold), catching genuine typos
               (transpositions, a wrong/missing letter) that don't collapse
               to the same normalized string. Sorted by ascending distance
               so the best guess leads. This tier NEVER auto-acts — see
               delete_event — over-matching here is safe because it can
               only ever produce a "did you mean?", never a wrong delete.

    Returns (status, tier, candidates): status is "ok" | "os_denied" |
    "error"; tier is "exact" | "approx" | "none" (only meaningful when
    status == "ok"). A calendar that's individually unreachable just
    narrows the candidate pool to whichever calendars DID respond (see
    _read_calendars_concurrent) rather than failing the whole search —
    status is only "error" if every calendar failed.

    writable_only=True (always, not a parameter here) — a read-only event
    (a holiday, a birthday) must NEVER be offered as a delete/move
    candidate; she can't act on it, so it's not a candidate no matter how
    well its title matches.
    """
    start_dt, end_dt = _resolve_delete_window(date_str)
    read = await _read_calendars_concurrent(start_dt, end_dt, writable_only=True)
    if read["status"] == "os_denied":
        return "os_denied", "none", []
    if read["status"] != "success":
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


def _window_result(status: str, events: list, message: str | None = None, unreachable: list | None = None) -> dict:
    result: dict = {"status": status, "events": events}
    if message is not None:
        result["message"] = message
    if unreachable:
        result["unreachable"] = unreachable
    return result


async def list_events_window(
    start_dt: datetime, end_dt: datetime, enabled: bool, writable_only: bool = True,
) -> dict:
    """Reads EVERY event across [start_dt, end_dt] via the same concurrent,
    per-calendar read _find_candidates uses for delete/move candidate
    search (_read_calendars_concurrent), minus the title matching: this
    returns every event in the window unfiltered, for a caller (the
    scheduler's placer) that needs the FULL busy set rather than a
    title-matched subset.

    `enabled` is the caller's already-resolved permission decision — same
    contract as every other public entry point here: no default, never
    silently allowed.

    `writable_only` defaults True and the scheduler's busy-set caller
    must never override it: a read-only calendar's event (a holiday, a
    birthday) is real and visible elsewhere, but it isn't hers to
    schedule around — leaving it OUT of the busy set is exactly how "an
    all-day holiday never blocks a slot" is enforced, not a special case
    the placer has to know about.

    Never raises. Returns
    {"status": "success"|"app_disabled"|"os_denied"|"error", "events": [...],
     "unreachable"?: [...], "message"?: str}
    — each event dict has the same shape list_events() returns:
    {uid, title, start, end, all_day, calendar}. A partial read (some
    calendars unreachable) still reports "success" with `unreachable`
    attached — see _read_calendars_concurrent's docstring.
    """
    if not enabled:
        return _window_result("app_disabled", [], "Calendar actions are turned off.")

    read = await _read_calendars_concurrent(start_dt, end_dt, writable_only=writable_only)
    if read["status"] == "os_denied":
        return _window_result(
            "os_denied", [], "This app doesn't have permission to access Calendar on macOS."
        )
    if read["status"] != "success":
        return _window_result("error", [], "Couldn't read the calendar.")

    events = read["events"]
    unreachable = read["unreachable"]
    log.info("[stage3] read %d event(s) across window %s -> %s%s",
             len(events), start_dt.isoformat(), end_dt.isoformat(),
             f" (unreachable: {unreachable})" if unreachable else "")
    return _window_result("success", events, unreachable=unreachable)


async def _check_recurrence(uid: str, calendar: str) -> tuple[str, dict]:
    """Reads the candidate's recurrence + title + start in one call (re-
    entering the calendar BY NAME, the -1728 fix, then `first`/`item 1 of
    (every event whose uid is X)`). Returns (status, info): status is
    "ok" | "not_found" | "os_denied" | "error" | "timeout"; info carries
    {"recurrence","title","start"} on "ok". A property-read failure inside
    the script is caught and reported as the "READ_ERROR" sentinel value
    (distinct from a genuinely empty — i.e. non-recurring — string) so the
    caller can fail safe rather than silently treating an unreadable
    property as "no recurrence"."""
    script = _LIST_APPLESCRIPT_HELPERS + f'''
tell application "Calendar"
    tell calendar "{_escape(calendar)}"
        set matches to (every event whose uid is "{uid}")
        if (count of matches) is 0 then
            return "{{\\"status\\":\\"NOT_FOUND\\"}}"
        end if
        set evt to item 1 of matches
        set evtRecurrence to "READ_ERROR"
        try
            set rawRecurrence to recurrence of evt
            if rawRecurrence is missing value then
                -- non-recurring events report `missing value` here, not an
                -- empty string — normalize so "" reliably means "no rule".
                set evtRecurrence to ""
            else
                set evtRecurrence to rawRecurrence
            end if
        end try
        set evtTitle to summary of evt
        set evtStart to my isoDate(start date of evt)
        return "{{\\"status\\":\\"OK\\",\\"recurrence\\":\\"" & my jsonEscape(evtRecurrence) & "\\",\\"title\\":\\"" & my jsonEscape(evtTitle) & "\\",\\"start\\":\\"" & evtStart & "\\"}}"
    end tell
end tell
'''
    code, out, err, timed_out = await _run_osa(script, _DELETE_TIMEOUT)
    if timed_out:
        return "timeout", {}
    if code != 0:
        return ("os_denied" if _is_os_denied(err) else "error"), {}
    try:
        payload = json.loads(out.strip())
    except (json.JSONDecodeError, ValueError):
        return "error", {}
    if payload.get("status") == "NOT_FOUND":
        return "not_found", {}
    return "ok", payload


async def _uid_count(uid: str, calendar: str) -> tuple[str, int]:
    """The spike's fast single-calendar re-verify primitive: `count of
    (every event whose uid is X)`, sub-second, safe to run immediately
    before every delete."""
    script = f'''
tell application "Calendar"
    tell calendar "{_escape(calendar)}"
        return (count of (every event whose uid is "{uid}")) as string
    end tell
end tell
'''
    code, out, err, timed_out = await _run_osa(script, _DELETE_TIMEOUT)
    if timed_out:
        return "timeout", -1
    if code != 0:
        return ("os_denied" if _is_os_denied(err) else "error"), -1
    try:
        return "ok", int(out.strip())
    except ValueError:
        return "error", -1


async def _delete_and_confirm(uid: str, calendar: str) -> tuple[str, int]:
    """The spike's verified delete-by-uid recipe (re-enter calendar BY NAME,
    delete item 1 of (every event whose uid is X), save) — plus an
    immediate recount of the same uid in the SAME call, so there's no gap
    between the delete and its confirmation. Returns (status, remaining)
    where status is "deleted" | "not_found" | "os_denied" | "error" |
    "timeout"."""
    script = f'''
tell application "Calendar"
    tell calendar "{_escape(calendar)}"
        set matches to (every event whose uid is "{uid}")
        if (count of matches) is 0 then
            return "NOT_FOUND|0"
        end if
        delete item 1 of matches
        save
        set remaining to (count of (every event whose uid is "{uid}"))
        return "DELETED|" & remaining
    end tell
end tell
'''
    code, out, err, timed_out = await _run_osa(script, _DELETE_TIMEOUT)
    if timed_out:
        return "timeout", -1
    if code != 0:
        return ("os_denied" if _is_os_denied(err) else "error"), -1
    parts = out.strip().split("|")
    if len(parts) != 2:
        return "error", -1
    status_word, count_str = parts
    try:
        remaining = int(count_str)
    except ValueError:
        remaining = -1
    if status_word == "NOT_FOUND":
        return "not_found", remaining
    if status_word == "DELETED":
        return "deleted", remaining
    return "error", remaining


async def delete_by_uid(uid: str, calendar: str, enabled: bool) -> dict:
    """The guarded single-event delete: app_disabled gate, recurrence
    refusal, re-verify, delete+confirm — extracted so BOTH callers share
    the exact same guards and there is no other path to a real delete
    anywhere in this module:
      1. delete_event()'s exact-unique-match auto-act path (below).
      2. The standalone follow-up-disambiguation endpoint (api/routes.py
         POST /calendar/delete_by_uid), which resolves a client-held
         "the 6pm one" / "yes, delete it" against a PRIOR ambiguous or
         approximate delete_event() result.

    `enabled` is the caller's already-resolved permission decision — same
    contract as every other public entry point here: no default, never
    silently allowed. The follow-up endpoint fetches permissions fresh on
    its own turn rather than trusting a decision cached from the original
    disambiguating turn.

    Never raises — every failure path returns a status dict.
    """
    if not enabled:
        return _delete_result("app_disabled", "Calendar actions are turned off.")

    try:
        rc_status, rc_info = await _check_recurrence(uid, calendar)
        if rc_status == "timeout":
            return _delete_result("error", "Calendar didn't respond in time.")
        if rc_status == "os_denied":
            return _delete_result(
                "os_denied", "This app doesn't have permission to access Calendar on macOS."
            )
        if rc_status == "not_found":
            return _delete_result("not_found", "couldn't find that event")
        if rc_status != "ok":
            return _delete_result("error", "Couldn't check that event.")

        title = rc_info.get("title") or ""
        start = rc_info.get("start") or ""

        # Fail-safe: "" is the only value that means genuinely non-recurring.
        # The READ_ERROR sentinel (property couldn't be read) refuses too —
        # never treat an unreadable recurrence as "safe".
        if rc_info.get("recurrence", "READ_ERROR") != "":
            log.info("[stage3] refusing recurring delete for uid=%s title=%r", uid, title)
            return _delete_result(
                "recurring_unsupported",
                "I can't delete repeating events yet — remove it in the Calendar app directly",
                title=title, start=start,
            )

        rv_status, rv_count = await _uid_count(uid, calendar)
        if rv_status == "timeout":
            return _delete_result("error", "Calendar didn't respond in time.")
        if rv_status == "os_denied":
            return _delete_result(
                "os_denied", "This app doesn't have permission to access Calendar on macOS."
            )
        if rv_status != "ok":
            return _delete_result("error", "Couldn't verify that event.")
        if rv_count == 0:
            return _delete_result("not_found", "that event's no longer there", title=title, start=start)

        del_status, remaining = await _delete_and_confirm(uid, calendar)
        if del_status == "timeout":
            return _delete_result("error", "Calendar didn't respond in time.")
        if del_status == "os_denied":
            return _delete_result(
                "os_denied", "This app doesn't have permission to access Calendar on macOS."
            )
        if del_status == "not_found":
            return _delete_result("not_found", "that event's no longer there", title=title, start=start)
        if del_status != "deleted" or remaining != 0:
            log.warning(
                "[stage3] delete did not confirm removal for uid=%s (remaining=%s) — "
                "reporting error, not success", uid, remaining,
            )
            return _delete_result("error", "couldn't remove it", title=title, start=start)

        log.info("[stage3] deleted event %r (uid=%s) from %r", title, uid, calendar)
        return _delete_result("success", title=title, start=start)

    except Exception as exc:
        log.error("[stage3] executor error deleting event uid=%s: %s", uid, exc)
        return _delete_result("error", "Couldn't delete that event.")


async def delete_event(args: dict, enabled: bool) -> dict:
    """Delete one specific event, identified either by an already-known
    uid+calendar (e.g. from a prior list_events() result — skips candidate
    search entirely) or by a title fragment to search for. An explicit
    date scopes that search to one day; no date widens it to today..+7
    days (see _resolve_delete_window) so two same-titled events on
    different days both surface as candidates instead of only whichever
    one an assumed single day happened to contain.

    Candidate resolution is TIERED (see _find_candidates):
      - EXACT match, exactly one  -> auto-act (delete_by_uid), no confirm.
      - EXACT match, more than one, or ANY approximate match (even a
        unique one) -> {status:"disambiguate", approximate, candidates}.
        Never deletes. The caller (chat.html, or a future voice client)
        must resolve this — by a direct follow-up the client can map
        deterministically, or by calling the delete_by_uid endpoint after
        the user confirms — before anything is actually removed. This is
        the one rule that does not bend: only a unique exact/normalized
        match may auto-act.
      - no match -> not_found.

    `enabled` is the caller's already-resolved permission decision — same
    contract as create_calendar_event/list_events: no default, never
    silently allowed.

    Never raises — every failure path returns a status dict. See the
    module docstring above delete_by_uid for why the guard order there is
    fixed and non-optional; this function does not alter it, only how the
    target uid+calendar is FOUND.
    """
    if not enabled:
        return _delete_result("app_disabled", "Calendar actions are turned off.")

    uid            = (args.get("uid") or "").strip()
    calendar       = (args.get("calendar") or "").strip()
    title_fragment = (args.get("title") or "").strip()

    if uid and calendar:
        return await delete_by_uid(uid, calendar, enabled)

    if not title_fragment:
        return _delete_result("error", "Couldn't tell which event to delete.")

    q_status, tier, candidates = await _find_candidates(title_fragment, args.get("date"))
    if q_status == "timeout":
        return _delete_result("error", "Calendar didn't respond in time.")
    if q_status == "os_denied":
        return _delete_result(
            "os_denied", "This app doesn't have permission to access Calendar on macOS."
        )
    if q_status != "ok":
        return _delete_result("error", "Couldn't search the calendar.")

    if tier == "none":
        return _delete_result("not_found", "couldn't find that event")

    if tier == "exact" and len(candidates) == 1:
        return await delete_by_uid(candidates[0]["uid"], candidates[0]["calendar"], enabled)

    # exact with >1 match, or ANY approximate match (even a unique one) —
    # must be confirmed by the caller before anything destructive happens.
    return _delete_result(
        "disambiguate",
        approximate=(tier == "approx"),
        candidates=[
            {"uid": c["uid"], "title": c.get("title"), "start": c.get("start"),
             "end": c.get("end"), "calendar": c.get("calendar"), "all_day": c.get("all_day", False)}
            for c in candidates
        ],
    )


# ---------------------------------------------------------------------------
# move_event — reschedules ONE specific event by uid. Target resolution
# (candidate search, exact/approximate tiers, ambiguity handling) is 100%
# shared with delete via _find_candidates — see that function's docstring.
# What's new here is entirely about the WRITE, driven by /tmp/cal_move_spike.py:
#
#   1. app_disabled if not enabled.
#   2. Resolve candidates exactly like delete_event (0 -> not_found, exact
#      match >1 or ANY approximate -> disambiguate, exactly 1 exact -> continue).
#   3. Recurrence refusal — reuses _check_recurrence, same fail-safe
#      contract as delete (an unreadable recurrence refuses too). UNLIKE
#      delete, this is NOT here to prevent a silent no-op — the spike found
#      the write PERSISTS on a recurring master, and silently shifts the
#      WHOLE recurring series (recurrence rule intact, base time moved) —
#      not the one occurrence the user meant. An apparently-successful
#      single-event move is actually an unrequested whole-series mutation.
#      Do not remove this guard thinking it's redundant with delete's; the
#      failure mode it prevents is the opposite one.
#   4. Re-verify the uid still exists immediately before writing (same
#      staleness guard as delete).
#   5. Compute the new end: an explicit new_end is used as given; otherwise
#      the event's ORIGINAL duration is read and preserved (new_end =
#      new_start + original_duration) — the spike found no auto-preserve
#      behavior at all, so this must always be computed and written
#      explicitly, never left to Calendar.app.
#   6. Write with SAFE FIELD ORDERING. The spike found Calendar.app
#      validates start<end against whatever is CURRENTLY set on the object
#      the instant a property is assigned, not the final values —
#      unconditionally writing `start date` before `end date` fails loudly
#      (AppleScript -10025 "The start date must be before the end date.")
#      any time the new start is later than the event's CURRENT
#      (not-yet-updated) end. The write compares the new start to the
#      live end date and picks the order that never creates a transient
#      invalid interval: start-then-end when the new start is still before
#      the live end, end-then-start otherwise.
#   7. Confirm read-back — a SEPARATE osascript call re-reads start/end and
#      compares to what was intended. No naturally-occurring silent no-op
#      was found for move in the spike (every write either persisted or
#      failed loudly), so this is defense-in-depth rather than the
#      load-bearing guard (that's 3+4, pre-write) — but a mismatch here is
#      still reported as "error", never surfaced as a false success.
# ---------------------------------------------------------------------------


def _move_result(
    status: str,
    message: str | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    candidates: list | None = None,
    approximate: bool | None = None,
    new_start: str | None = None,
    new_end: str | None = None,
) -> dict:
    result: dict = {"status": status, "kind": "move"}
    if message is not None:
        result["message"] = message
    if title is not None:
        result["title"] = title
    if start is not None:
        result["start"] = start
    if end is not None:
        result["end"] = end
    if all_day is not None:
        result["all_day"] = all_day
    if candidates is not None:
        result["candidates"] = candidates
    if approximate is not None:
        result["approximate"] = approximate
    if new_start is not None:
        result["new_start"] = new_start
    if new_end is not None:
        result["new_end"] = new_end
    return result


async def _read_times(uid: str, calendar: str) -> tuple[str, dict]:
    """Full title/start/end/all_day read-back by uid, re-entering the
    calendar BY NAME first (the -1728 fix). Used for two different reasons
    by move_by_uid: computing the ORIGINAL duration when no explicit
    new_end is given, and as the post-write confirm read-back — always a
    SEPARATE osascript call from whatever wrote it, never trusting the
    write command's own report (same discipline as delete's recount).
    Returns (status, info): status is
    "ok" | "not_found" | "os_denied" | "error" | "timeout"."""
    script = _LIST_APPLESCRIPT_HELPERS + f'''
tell application "Calendar"
    tell calendar "{_escape(calendar)}"
        set matches to (every event whose uid is "{uid}")
        if (count of matches) is 0 then
            return "{{\\"status\\":\\"NOT_FOUND\\"}}"
        end if
        set evt to item 1 of matches
        return "{{\\"status\\":\\"OK\\",\\"title\\":\\"" & my jsonEscape(summary of evt) & "\\",\\"start\\":\\"" & my isoDate(start date of evt) & "\\",\\"end\\":\\"" & my isoDate(end date of evt) & "\\",\\"all_day\\":" & (allday event of evt as string) & "}}"
    end tell
end tell
'''
    code, out, err, timed_out = await _run_osa(script, _DELETE_TIMEOUT)
    if timed_out:
        return "timeout", {}
    if code != 0:
        return ("os_denied" if _is_os_denied(err) else "error"), {}
    try:
        payload = json.loads(out.strip())
    except (json.JSONDecodeError, ValueError):
        return "error", {}
    if payload.get("status") == "NOT_FOUND":
        return "not_found", {}
    return "ok", payload


async def _write_move(uid: str, calendar: str, new_start: datetime, new_end: datetime) -> str:
    """The spike's verified write-ordering fix for the -10025 "start date
    must be before end date" trap: compares the new start to the event's
    LIVE end date (read inside this same script) and orders the two
    assignments so a transient invalid interval is never created — see the
    module comment above move_event for the full rationale. Returns
    "ok" | "not_found" | "os_denied" | "error" | "timeout"."""
    script = (
        _applescript_date_setter("newStart", new_start)
        + _applescript_date_setter("newEnd", new_end)
        + f'''
tell application "Calendar"
    tell calendar "{_escape(calendar)}"
        set matches to (every event whose uid is "{uid}")
        if (count of matches) is 0 then
            return "NOT_FOUND"
        end if
        set evt to item 1 of matches
        set oldEnd to end date of evt
        if newStart < oldEnd then
            set start date of evt to newStart
            set end date of evt to newEnd
        else
            set end date of evt to newEnd
            set start date of evt to newStart
        end if
        save
        return "OK"
    end tell
end tell
'''
    )
    code, out, err, timed_out = await _run_osa(script, _DELETE_TIMEOUT)
    if timed_out:
        return "timeout"
    if code != 0:
        return "os_denied" if _is_os_denied(err) else "error"
    result = out.strip()
    if result == "NOT_FOUND":
        return "not_found"
    if result == "OK":
        return "ok"
    return "error"


async def move_by_uid(
    uid: str, calendar: str, new_start: str, new_end: str | None, enabled: bool
) -> dict:
    """The guarded single-event move: app_disabled gate, recurrence
    refusal, re-verify, duration-preserving new_end computation, the
    safe-ordered write, and a confirm read-back — extracted so BOTH
    callers share the exact same guards and there is no other path to a
    real write anywhere in this module:
      1. move_event()'s exact-unique-match auto-act path (below).
      2. The standalone follow-up-disambiguation endpoint (api/routes.py
         POST /calendar/move_by_uid), which resolves a client-held
         "the 6pm one" / "yes, move it" against a PRIOR ambiguous or
         approximate move_event() result, carrying the originally-
         requested new_start/new_end through the disambiguation turn.

    `new_start`/`new_end` are raw ISO 8601 strings (not pre-parsed) — same
    calling convention as every other public entry point's args dict, so
    callers never need this module's private date parsing.

    `enabled` is the caller's already-resolved permission decision — same
    contract as delete_by_uid: no default, never silently allowed. The
    follow-up endpoint fetches permissions fresh on its own turn rather
    than trusting a decision cached from the original disambiguating turn.

    Never raises — every failure path returns a status dict.
    """
    if not enabled:
        return _move_result("app_disabled", "Calendar actions are turned off.")

    new_start_dt = _parse_dt((new_start or "").strip())
    if new_start_dt is None:
        return _move_result("error", "Couldn't tell what time to move it to.")
    new_end_dt = _parse_dt((new_end or "").strip()) if new_end else None

    try:
        rc_status, rc_info = await _check_recurrence(uid, calendar)
        if rc_status == "timeout":
            return _move_result("error", "Calendar didn't respond in time.")
        if rc_status == "os_denied":
            return _move_result(
                "os_denied", "This app doesn't have permission to access Calendar on macOS."
            )
        if rc_status == "not_found":
            return _move_result("not_found", "couldn't find that event")
        if rc_status != "ok":
            return _move_result("error", "Couldn't check that event.")

        title = rc_info.get("title") or ""
        orig_start = rc_info.get("start") or ""

        # Fail-safe, same contract as delete: "" is the only value that
        # means genuinely non-recurring; READ_ERROR (unreadable property)
        # refuses too. See the module comment above move_event for WHY
        # this guard exists — it is not the same reason as delete's.
        if rc_info.get("recurrence", "READ_ERROR") != "":
            log.info("[stage3] refusing recurring move for uid=%s title=%r", uid, title)
            return _move_result(
                "recurring_unsupported",
                "I can't move repeating events yet — change it in the Calendar app directly",
                title=title, start=orig_start,
            )

        rv_status, rv_count = await _uid_count(uid, calendar)
        if rv_status == "timeout":
            return _move_result("error", "Calendar didn't respond in time.")
        if rv_status == "os_denied":
            return _move_result(
                "os_denied", "This app doesn't have permission to access Calendar on macOS."
            )
        if rv_status != "ok":
            return _move_result("error", "Couldn't verify that event.")
        if rv_count == 0:
            return _move_result("not_found", "that event's no longer there", title=title, start=orig_start)

        if new_end_dt is None:
            times_status, times_info = await _read_times(uid, calendar)
            if times_status == "timeout":
                return _move_result("error", "Calendar didn't respond in time.")
            if times_status == "os_denied":
                return _move_result(
                    "os_denied", "This app doesn't have permission to access Calendar on macOS."
                )
            if times_status != "ok":
                return _move_result("error", "Couldn't read that event's current time.", title=title)
            orig_start_dt = _parse_dt(times_info.get("start", ""))
            orig_end_dt = _parse_dt(times_info.get("end", ""))
            if orig_start_dt is None or orig_end_dt is None:
                return _move_result("error", "Couldn't read that event's current time.", title=title)
            new_end_dt = new_start_dt + (orig_end_dt - orig_start_dt)

        write_status = await _write_move(uid, calendar, new_start_dt, new_end_dt)
        if write_status == "timeout":
            return _move_result("error", "Calendar didn't respond in time.")
        if write_status == "os_denied":
            return _move_result(
                "os_denied", "This app doesn't have permission to access Calendar on macOS."
            )
        if write_status == "not_found":
            return _move_result("not_found", "that event's no longer there", title=title, start=orig_start)
        if write_status != "ok":
            # Covers -10025 ("start date must be before end date") and any
            # other AppleScript save-validation error — loud, never silent,
            # never surfaced as success.
            return _move_result("error", "couldn't move it", title=title, start=orig_start)

        confirm_status, confirm_info = await _read_times(uid, calendar)
        if confirm_status == "timeout":
            return _move_result("error", "Calendar didn't respond in time.")
        if confirm_status == "os_denied":
            return _move_result(
                "os_denied", "This app doesn't have permission to access Calendar on macOS."
            )
        if confirm_status != "ok":
            return _move_result("error", "Couldn't confirm the move.", title=title)

        confirmed_start = _parse_dt(confirm_info.get("start", ""))
        confirmed_end = _parse_dt(confirm_info.get("end", ""))
        if confirmed_start != new_start_dt or confirmed_end != new_end_dt:
            # Defense-in-depth: the spike found no naturally-occurring
            # silent no-op for move, but a mismatch here means the write
            # did not actually take — never report success on a guess.
            log.warning(
                "[stage3] move did not confirm for uid=%s (intended %s-%s, actual %s-%s) — "
                "reporting error, not success", uid, new_start_dt, new_end_dt, confirmed_start, confirmed_end,
            )
            return _move_result("error", "couldn't confirm the move", title=title)

        log.info("[stage3] moved event %r (uid=%s) to %s -> %s",
                  title, uid, new_start_dt.isoformat(), new_end_dt.isoformat())
        return _move_result(
            "success", title=title,
            start=new_start_dt.isoformat(), end=new_end_dt.isoformat(),
            all_day=confirm_info.get("all_day", False),
        )

    except Exception as exc:
        log.error("[stage3] executor error moving event uid=%s: %s", uid, exc)
        return _move_result("error", "Couldn't move that event.")


async def move_event(args: dict, enabled: bool) -> dict:
    """Move (reschedule) one specific event, identified either by an
    already-known uid+calendar (skips candidate search entirely) or by a
    title fragment to search for — the EXACT same target-resolution
    contract as delete_event, reusing _find_candidates verbatim (see its
    docstring for the exact/approximate tier rules). Candidate resolution
    never differs between delete and move; only what happens to the
    resolved event does — see the module comment above move_event's
    section header for the write-side guards.

    `enabled` is the caller's already-resolved permission decision — same
    contract as delete_event/create_calendar_event/list_events: no
    default, never silently allowed.

    Never raises — every failure path returns a status dict.
    """
    if not enabled:
        return _move_result("app_disabled", "Calendar actions are turned off.")

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
        return await move_by_uid(uid, calendar, new_start_s, new_end_s, enabled)

    if not title_fragment:
        return _move_result("error", "Couldn't tell which event to move.")

    q_status, tier, candidates = await _find_candidates(title_fragment, args.get("date"))
    if q_status == "timeout":
        return _move_result("error", "Calendar didn't respond in time.")
    if q_status == "os_denied":
        return _move_result(
            "os_denied", "This app doesn't have permission to access Calendar on macOS."
        )
    if q_status != "ok":
        return _move_result("error", "Couldn't search the calendar.")

    if tier == "none":
        return _move_result("not_found", "couldn't find that event")

    if tier == "exact" and len(candidates) == 1:
        return await move_by_uid(
            candidates[0]["uid"], candidates[0]["calendar"], new_start_s, new_end_s, enabled
        )

    # exact with >1 match, or ANY approximate match (even a unique one) —
    # must be confirmed by the caller before anything is written. The
    # originally-requested new_start/new_end ride along so the caller can
    # apply the SAME intended time once a candidate is picked — see
    # move_by_uid's docstring for how the follow-up endpoint uses these.
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
