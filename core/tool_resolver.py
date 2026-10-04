"""
Stage 2 tool resolver — parses a plain-English calendar action_request (produced by the
chat model's <action> signal) into a typed function call.

Scope: resolve only. No execution, no AppleScript, no OAuth, no calendar API.
Timezone: start/end are local naive ISO 8601 strings (no offset). Timezone handling
is deferred to the executor stage.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from google.genai import types

from core.gemini import gemini_client, SAFETY_SETTINGS

log = logging.getLogger(__name__)

_RESOLVER_MODEL = "gemini-2.5-flash"
_TIMEOUT        = 15.0

# ---------------------------------------------------------------------------
# Schema — minimal but extensible. Extend here when the executor gains more
# fields; the resolver and executor share this single source of truth.
# ---------------------------------------------------------------------------

_CREATE_EVENT = types.FunctionDeclaration(
    name="create_event",
    description="Create a new calendar event",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "title":   types.Schema(type=types.Type.STRING,
                                    description="Event title"),
            "start":   types.Schema(type=types.Type.STRING,
                                    description="ISO 8601 local datetime, e.g. 2026-08-09T19:00:00"),
            "end":     types.Schema(type=types.Type.STRING,
                                    description="ISO 8601 local datetime for the end (optional)"),
            "all_day": types.Schema(type=types.Type.BOOLEAN,
                                    description="True if this is an all-day event"),
        },
        required=["title", "start"],
    ),
)

_LIST_EVENTS = types.FunctionDeclaration(
    name="list_events",
    description="List the events on the calendar for a given day, or across a multi-day range",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "date": types.Schema(type=types.Type.STRING,
                                 description="ISO 8601 date, e.g. 2026-08-09 — the SINGLE day to "
                                             "list; omit for tomorrow. Use this OR window_start/"
                                             "window_end, never both — a single named day (or no "
                                             "day at all) uses this; a range ('this week', 'this "
                                             "weekend') uses the window fields instead."),
            "window_start": types.Schema(type=types.Type.STRING,
                                         description="ISO 8601 date — first day of a multi-day "
                                                     "range ('this week' -> today; 'this weekend' "
                                                     "-> the coming Saturday). Only for a request "
                                                     "that spans more than one day; omit for a "
                                                     "single-day request."),
            "window_end": types.Schema(type=types.Type.STRING,
                                       description="ISO 8601 date — last day of the range "
                                                   "('this week' -> window_start+6 days; 'this "
                                                   "weekend' -> the coming Sunday). Required "
                                                   "together with window_start; omit for a "
                                                   "single-day request."),
        },
        required=[],
    ),
)

_DELETE_EVENT = types.FunctionDeclaration(
    name="delete_event",
    description="Delete an existing calendar event",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "title": types.Schema(type=types.Type.STRING,
                                  description="Title or a distinctive fragment of the event to "
                                              "delete — not the whole request sentence"),
            "date":  types.Schema(type=types.Type.STRING,
                                  description="ISO 8601 date, e.g. 2026-08-09 — the day the event "
                                              "is on; omit for tomorrow"),
        },
        required=["title"],
    ),
)

_MOVE_EVENT = types.FunctionDeclaration(
    name="move_event",
    description="Move (reschedule) an existing calendar event to a new time",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "title":     types.Schema(type=types.Type.STRING,
                                      description="Title or a distinctive fragment of the event to "
                                                  "move — not the whole request sentence"),
            "date":      types.Schema(type=types.Type.STRING,
                                      description="ISO 8601 date, e.g. 2026-08-09 — the day the "
                                                  "event is CURRENTLY on; omit if not stated"),
            "new_start": types.Schema(type=types.Type.STRING,
                                      description="ISO 8601 local datetime to move the event TO, "
                                                  "e.g. 2026-08-09T20:00:00"),
            "new_end":   types.Schema(type=types.Type.STRING,
                                      description="ISO 8601 local datetime for the new end — only "
                                                  "if explicitly stated; omit to preserve the "
                                                  "event's original duration automatically"),
        },
        required=["title", "new_start"],
    ),
)

_SUGGEST_SLOTS = types.FunctionDeclaration(
    name="suggest_slots",
    description="Suggest candidate free time slots for an activity, for the user to review and accept",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "activity": types.Schema(type=types.Type.STRING,
                                     description="The activity to schedule, in the user's own words "
                                                 "(e.g. 'gym', not 'gym session')"),
            "count": types.Schema(type=types.Type.INTEGER,
                                  description="How many slots to find — infer from the request "
                                              "('6x' -> 6); default to 1 if not stated"),
            "window_start": types.Schema(type=types.Type.STRING,
                                         description="ISO 8601 date, e.g. 2026-08-09 — first day to "
                                                     "search, resolved against the current date "
                                                     "('this week' -> today; 'next 3 days' -> today)"),
            "window_end": types.Schema(type=types.Type.STRING,
                                       description="ISO 8601 date — last day to search, resolved "
                                                   "against window_start ('this week' -> +6 days; "
                                                   "'next 3 days' -> +2 days)"),
            "stated_duration_min": types.Schema(type=types.Type.INTEGER,
                                                description="Lower bound of a stated duration in "
                                                            "minutes, if given (e.g. '1-1.5h' -> 60); "
                                                            "omit if no duration was stated"),
            "stated_duration_max": types.Schema(type=types.Type.INTEGER,
                                                description="Upper bound of a stated duration in "
                                                            "minutes, if given (e.g. '1-1.5h' -> 90); "
                                                            "omit if no duration was stated"),
            "tod_pref": types.Schema(type=types.Type.STRING,
                                     description="'morning', 'afternoon', or 'evening' if the user "
                                                 "stated one; omit otherwise"),
            "day_exclusions": types.Schema(
                type=types.Type.ARRAY,
                description="ISO 8601 dates the user explicitly excluded (e.g. 'not Friday'); omit "
                            "if none",
                items=types.Schema(type=types.Type.STRING),
            ),
        },
        required=["activity", "window_start", "window_end"],
    ),
)

_TOOL = types.Tool(function_declarations=[
    _CREATE_EVENT, _LIST_EVENTS, _DELETE_EVENT, _MOVE_EVENT, _SUGGEST_SLOTS,
])

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ResolvedAction:
    status: str        # "resolved" | "unsupported" | "error"
    tool:   str | None # "create_event" | "list_events" | "delete_event" | "move_event" | "suggest_slots" when resolved, else None
    args:   dict | None
    raw:    str        # original action_request, for logging/debug


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def _system_instruction(now: datetime) -> str:
    # Real local zone abbreviation (e.g. "EDT"), not a hardcoded guess —
    # this used to always say "Pacific Time" regardless of the machine's
    # actual timezone. `now` itself was always correct local time; only
    # this label was ever wrong, so fixing it doesn't shift any date/
    # window computation anywhere.
    tz_name = now.astimezone().strftime("%Z") or "local time"
    today_human = now.strftime(f"%A, %B %d, %Y at %I:%M %p {tz_name}")
    today_iso   = now.strftime("%Y-%m-%d")
    tomorrow    = (now + timedelta(days=1)).strftime("%A, %B %d, %Y")
    return (
        f"Today is {today_human} (ISO date: {today_iso}). Tomorrow is {tomorrow}.\n\n"
        "You can take a real action by calling one of the provided functions: "
        "create_event, list_events, delete_event, move_event, or suggest_slots.\n\n"
        "RULES — follow them exactly:\n"
        "1. If this is a request to CREATE, ADD, SCHEDULE, SET UP, or PUT a NEW event on "
        "the calendar: you MUST call create_event. Fill every argument you can reasonably "
        "infer from the text. Do NOT ask clarifying questions. Do NOT describe what you "
        "would do — actually call the function.\n"
        "2. If this is a request to LIST, SHOW, or CHECK the calendar, or to ask what's on "
        "it — for a SINGLE day OR a MULTI-DAY range — you MUST call list_events. NEVER refuse "
        "or reply UNSUPPORTED just because the request spans more than one day — every read "
        "request, single-day or ranged, has a real path here.\n"
        "   - SINGLE DAY ('today', 'tomorrow', 'friday', or no day named at all): fill the "
        "date argument (ISO YYYY-MM-DD) resolved against the current date given above; omit "
        "date entirely if the request says 'tomorrow' or doesn't name a day — it defaults to "
        "tomorrow. Do NOT fill window_start/window_end for a single day.\n"
        "   - MULTI-DAY RANGE ('this week', 'this weekend', 'the next few days', 'what's "
        "coming up', 'what do I have going on'): fill window_start and window_end (ISO "
        "YYYY-MM-DD) instead of date. 'this week' means window_start = today, window_end = "
        "today+6 days. 'this weekend' means window_start = the coming Saturday (today if "
        "today IS Saturday), window_end = the coming Sunday. 'the next N days' means "
        "window_start = today, window_end = today+(N-1) days. A VAGUE range with no number or "
        "named unit ('what's coming up', 'anything going on', 'what do I have going on') "
        "still gets a window — default it to window_start = today, window_end = today+6 days, "
        "same as 'this week'. Do NOT fill date for a range.\n"
        "   Do NOT describe what you would do — actually call the function.\n"
        "3. If this is a request to DELETE, REMOVE, or CANCEL an EXISTING event: you MUST "
        "call delete_event. Fill title with the user's ACTUAL words for the event name — NOT "
        "the whole sentence, and NEVER a generic noun they didn't say ('gym' stays 'gym', "
        "never 'gym event' or 'gym meeting' — the tool matches this text against the real "
        "calendar title, so adding words that aren't there makes it match nothing). Fill "
        "date only if they named a day (ISO YYYY-MM-DD); omit date if they didn't — the tool "
        "searches the next several days for it. Do NOT ask which one if the request is vague "
        "— the tool handles ambiguity itself. Do NOT describe what you would do — actually "
        "call the function.\n"
        "4. If this is a request to MOVE, RESCHEDULE, or CHANGE the time of an EXISTING "
        "event, including phrasing like 'push X to Y': you MUST call move_event. Fill title "
        "with the user's ACTUAL words for the event name, same rule as delete_event — NEVER "
        "a generic noun they didn't say. Fill new_start with the ISO 8601 datetime they want "
        "it moved TO, resolved against the current date given above. Fill new_end ONLY if "
        "they explicitly stated a new end time or duration; omit it otherwise — the tool "
        "preserves the event's original duration automatically. Fill date only if they named "
        "which day the event is CURRENTLY on; omit it otherwise — the tool searches the next "
        "several days for it. Do NOT ask which one if the request is vague — the tool handles "
        "ambiguity itself. Do NOT describe what you would do — actually call the function.\n"
        "5. If this is a request to SUGGEST, FIND, or HELP FIND time for an activity — 'find me "
        "gym times', 'when can I fit in a haircut', 'help me schedule laundry', 'suggest a time "
        "for X' — you MUST call suggest_slots. This is for finding NEW time for an activity the "
        "user describes, not for a specific already-named event on the calendar (that's "
        "create/move). Fill activity with the user's own words for the activity. Fill count from "
        "how many times they asked for ('6x', 'six times' -> 6); default to 1 if they didn't say. "
        "Resolve window_start/window_end against the current date given above — 'this week' means "
        "window_start = today, window_end = today+6 days; 'next 3 days' means window_start = today, "
        "window_end = today+2 days; a specific range they name overrides these defaults. Fill "
        "stated_duration_min/max ONLY if they gave a duration or range (e.g. '1-1.5 hours' -> "
        "min=60, max=90) — omit both otherwise, the tool infers a sensible default. Fill tod_pref "
        "only if they said a time of day (morning/afternoon/evening). Fill day_exclusions only if "
        "they explicitly ruled out specific days. Do NOT ask clarifying questions — the tool "
        "handles gaps in what's known. Do NOT describe what you would do — actually call the "
        "function.\n"
        "6. Resolve all relative dates ('tomorrow', 'next monday', 'friday', 'this weekend') "
        "against the current date given above. NEVER output a date that is in the past. If the "
        f"request says 'today', use EXACTLY {today_iso} — do NOT advance to the next day.\n"
        "7. Never output anything other than either a function call or the word UNSUPPORTED."
    )


def _parse_iso(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _validate(tool: str, args: dict, now: datetime) -> str | None:
    """Return an error string if args are invalid for this tool, else None."""
    if tool == "create_event":
        title = args.get("title", "").strip()
        start = args.get("start", "").strip()
        if not title:
            return "title is empty"
        if not start:
            return "start is empty"
        dt = _parse_iso(start)
        if dt is None:
            return f"start {start!r} is not a parseable ISO 8601 datetime"
        if dt.date() < now.date():
            return f"start {start!r} is in the past (today is {now.date()})"
        return None

    if tool == "list_events":
        window_start = args.get("window_start", "").strip()
        window_end = args.get("window_end", "").strip()
        if window_start or window_end:
            if not window_start:
                return "window_start is empty but window_end was given"
            if not window_end:
                return "window_end is empty but window_start was given"
            start_dt = _parse_iso(window_start)
            if start_dt is None:
                return f"window_start {window_start!r} is not a parseable ISO 8601 date"
            end_dt = _parse_iso(window_end)
            if end_dt is None:
                return f"window_end {window_end!r} is not a parseable ISO 8601 date"
            if end_dt.date() < now.date():
                return f"window_end {window_end!r} is entirely in the past (today is {now.date()})"
            if end_dt.date() < start_dt.date():
                return f"window_end {window_end!r} is before window_start {window_start!r}"
            return None

        date_str = args.get("date", "").strip()
        if not date_str:
            return None   # omitted -> executor defaults to tomorrow
        dt = _parse_iso(date_str)
        if dt is None:
            return f"date {date_str!r} is not a parseable ISO 8601 date"
        if dt.date() < now.date():
            return f"date {date_str!r} is in the past (today is {now.date()})"
        return None

    if tool == "delete_event":
        title = args.get("title", "").strip()
        if not title:
            return "title is empty"
        date_str = args.get("date", "").strip()
        if not date_str:
            return None   # omitted -> executor defaults to tomorrow
        dt = _parse_iso(date_str)
        if dt is None:
            return f"date {date_str!r} is not a parseable ISO 8601 date"
        if dt.date() < now.date():
            return f"date {date_str!r} is in the past (today is {now.date()})"
        return None

    if tool == "move_event":
        title = args.get("title", "").strip()
        if not title:
            return "title is empty"
        new_start = args.get("new_start", "").strip()
        if not new_start:
            return "new_start is empty"
        if _parse_iso(new_start) is None:
            return f"new_start {new_start!r} is not a parseable ISO 8601 datetime"
        date_str = args.get("date", "").strip()
        if date_str and _parse_iso(date_str) is None:
            return f"date {date_str!r} is not a parseable ISO 8601 date"
        new_end = args.get("new_end", "").strip()
        if new_end and _parse_iso(new_end) is None:
            return f"new_end {new_end!r} is not a parseable ISO 8601 datetime"
        return None

    if tool == "suggest_slots":
        activity = args.get("activity", "").strip()
        if not activity:
            return "activity is empty"
        count = args.get("count", 1)
        try:
            count = int(count)
        except (TypeError, ValueError):
            return f"count {count!r} is not an integer"
        if count < 1:
            return f"count {count!r} must be at least 1"
        window_start = args.get("window_start", "").strip()
        window_end = args.get("window_end", "").strip()
        if not window_start:
            return "window_start is empty"
        if not window_end:
            return "window_end is empty"
        start_dt = _parse_iso(window_start)
        if start_dt is None:
            return f"window_start {window_start!r} is not a parseable ISO 8601 date"
        end_dt = _parse_iso(window_end)
        if end_dt is None:
            return f"window_end {window_end!r} is not a parseable ISO 8601 date"
        if end_dt.date() < now.date():
            return f"window_end {window_end!r} is entirely in the past (today is {now.date()})"
        if end_dt.date() < start_dt.date():
            return f"window_end {window_end!r} is before window_start {window_start!r}"
        return None

    return f"unknown tool {tool!r}"


async def resolve_calendar_action(
    action_request: str,
    now: datetime | None = None,
) -> ResolvedAction:
    """Parse a plain-English calendar request into a ResolvedAction.

    Never raises — all exceptions are caught and returned as status="error".
    """
    if now is None:
        now = datetime.now()

    try:
        resp = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=_RESOLVER_MODEL,
                contents=[types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=action_request)],
                )],
                config=types.GenerateContentConfig(
                    system_instruction=_system_instruction(now),
                    tools=[_TOOL],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.AUTO,
                        )
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    temperature=0,
                    safety_settings=SAFETY_SETTINGS,
                ),
            ),
            timeout=_TIMEOUT,
        )

        fcs = resp.function_calls
        if fcs:
            fc   = fcs[0]
            tool = fc.name
            args = dict(fc.args)
            err  = _validate(tool, args, now)
            if err:
                log.info("[stage2] validation error for %r (%s): %s", action_request, tool, err)
                return ResolvedAction(status="error", tool=None, args=None, raw=action_request)
            log.info("[stage2] resolved %s: %r", tool, args)
            return ResolvedAction(status="resolved", tool=tool, args=args,
                                  raw=action_request)

        # No function call — expect the word UNSUPPORTED
        text = (resp.text or "").strip()
        log.info("[stage2] unsupported action %r (model replied: %r)", action_request, text[:80])
        return ResolvedAction(status="unsupported", tool=None, args=None, raw=action_request)

    except Exception as exc:
        log.error("[stage2] resolver error for %r: %s", action_request, exc)
        return ResolvedAction(status="error", tool=None, args=None, raw=action_request)


# ---------------------------------------------------------------------------
# __main__ test — requires live creds
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    os.environ.setdefault(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.path.join(os.path.dirname(__file__), "..", "google-key.json"),
    )

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cases = [
        "dinner with Adam tomorrow at 7pm",
        "add gym friday morning",
        "schedule dentist on Aug 20 2026 2pm",
        "what's on my calendar tomorrow",
        "what's on my calendar today",
        "show me friday",
        "delete my 3pm meeting",
        "cancel dinner",
        "move dinner to 8pm",
    ]

    async def run():
        for case in cases:
            result = await resolve_calendar_action(case)
            print(f"\ninput:  {case!r}")
            print(f"result: {result}")

    asyncio.run(run())
