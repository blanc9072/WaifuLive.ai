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

_TOOL = types.Tool(function_declarations=[_CREATE_EVENT])

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ResolvedAction:
    status: str        # "resolved" | "unsupported" | "error"
    tool:   str | None # "create_event" when resolved, else None
    args:   dict | None
    raw:    str        # original action_request, for logging/debug


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def _system_instruction(now: datetime) -> str:
    today_human = now.strftime("%A, %B %d, %Y at %I:%M %p Pacific Time")
    today_iso   = now.strftime("%Y-%m-%d")
    tomorrow    = (now + timedelta(days=1)).strftime("%A, %B %d, %Y")
    return (
        f"Today is {today_human} (ISO date: {today_iso}). Tomorrow is {tomorrow}.\n\n"
        "You can take a real action by calling the provided create_event function.\n\n"
        "RULES — follow them exactly:\n"
        "1. If this is a request to CREATE, ADD, SCHEDULE, SET UP, or PUT a NEW event on "
        "the calendar: you MUST call create_event. Fill every argument you can reasonably "
        "infer from the text. Do NOT ask clarifying questions. Do NOT describe what you "
        "would do — actually call the function.\n"
        "2. Resolve all relative dates ('tomorrow', 'next monday', 'friday', 'this weekend') "
        "against the current date given above. NEVER output a date that is in the past.\n"
        "3. If the request is ANY OTHER calendar action — delete, remove, cancel, move, "
        "reschedule, change, list, show, check, or ask what is on the calendar — do NOT "
        "call any function. Reply with the single word: UNSUPPORTED\n"
        "4. Never output anything other than either a function call or the word UNSUPPORTED."
    )


def _parse_iso(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _validate(args: dict, now: datetime) -> str | None:
    """Return an error string if args are invalid, else None."""
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
            args = dict(fc.args)
            err  = _validate(args, now)
            if err:
                log.info("[stage2] validation error for %r: %s", action_request, err)
                return ResolvedAction(status="error", tool=None, args=None, raw=action_request)
            log.info("[stage2] resolved create_event: %r", args)
            return ResolvedAction(status="resolved", tool="create_event", args=args,
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
        "delete my 3pm meeting",
        "what's on my calendar today",
    ]

    async def run():
        for case in cases:
            result = await resolve_calendar_action(case)
            print(f"\ninput:  {case!r}")
            print(f"result: {result}")

    asyncio.run(run())
