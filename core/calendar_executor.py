"""
Stage 3 — executes a resolved create_event action against macOS Calendar via
AppleScript (osascript).

Standalone and model-agnostic: takes a plain args dict, returns a plain dict.
Deliberately does NOT import routes, request/response models, or anything
voice/Live-specific, so the voice relay can reuse create_calendar_event()
unchanged once that path is wired up.

Gating (checked in order, default-deny):
  1. calendar_enabled() — app-level feature flag (CALENDAR_ENABLED env var).
  2. macOS Calendar automation permission, enforced by the OS.

The permission-denied signature below was captured live on this machine by
revoking Calendar automation for the requesting app (VS Code) and rerunning
osascript: exit code 1, stderr
  "execution error: Not authorized to send Apple events to Calendar. (-1743)"
Matched loosely (substring + code) since exact wording can vary by macOS version.
"""

import asyncio
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_TIMEOUT = 10.0
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


def calendar_enabled() -> bool:
    """App-level feature flag. Default-deny: unset/unreadable/false -> disabled."""
    try:
        return os.getenv("CALENDAR_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


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


async def create_calendar_event(args: dict) -> dict:
    """Create a calendar event from a resolved create_event args dict.

    Never raises — every failure path returns a status dict. Synchronous
    callers (e.g. /chat) can await this without risking the turn; on
    timeout or any executor failure this returns status="error".
    """
    title   = (args.get("title") or "").strip()
    start_s = (args.get("start") or "").strip()
    end_s   = (args.get("end") or "").strip()
    all_day = bool(args.get("all_day", False))

    if not calendar_enabled():
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
