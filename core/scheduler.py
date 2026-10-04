"""
Scheduler — Part B (activity inference) and Part C (deterministic placer).

THE INVARIANT (do not violate — this is the whole safety story):
The model reasons about the ACTIVITY (calendar-blind). Deterministic code
reasons about the CALENDAR. They never cross.

  - infer_block() sees only the activity name, what the user said about
    duration, and a stored preference row. It NEVER sees the calendar, so
    it is structurally incapable of confabulating a schedule.
  - place_slots() sees a real busy set (the caller's job to fetch it via a
    live list_events() read) and can ONLY select start times that fall
    inside a computed free gap — so it is structurally incapable of
    proposing a double-book, no matter what infer_block said.

Neither function calls the other, and place_slots takes no model/network
dependency at all — it is a pure function, deliberately kept that way so
it can be property-tested with a fake busy set ("never returns a slot
overlapping a busy interval" is checkable exhaustively, not just by eye).
"""

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta

from google.genai import types

from core.gemini import gemini_client, SAFETY_SETTINGS

log = logging.getLogger(__name__)

_SCHEDULER_MODEL = "gemini-2.5-flash"
_INFER_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# Part B — infer_block (model, CALENDAR-BLIND)
# ---------------------------------------------------------------------------

_INFER_BLOCK = types.FunctionDeclaration(
    name="infer_block",
    description="Infer the true time block an activity needs, including any implicit prep/commute/cleanup",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "block_min": types.Schema(
                type=types.Type.INTEGER,
                description="Total minutes to block off, including all prep/commute/cleanup"),
            "default_duration_min": types.Schema(
                type=types.Type.INTEGER,
                description="Minutes for the core activity itself, excluding padding"),
            "pad_before_min": types.Schema(
                type=types.Type.INTEGER,
                description="Minutes of commute/setup needed before the activity starts"),
            "pad_after_min": types.Schema(
                type=types.Type.INTEGER,
                description="Minutes of cleanup/commute needed after the activity ends"),
            "tod_pref": types.Schema(
                type=types.Type.STRING,
                description="'morning', 'afternoon', or 'evening' if the activity has a natural "
                            "time-of-day fit; omit entirely if it doesn't"),
            "breakdown": types.Schema(
                type=types.Type.ARRAY,
                description="Ordered {label, minutes} pairs that sum to block_min, e.g. "
                            "[{\"label\":\"gym\",\"minutes\":90},{\"label\":\"commute\",\"minutes\":40}]",
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "label":   types.Schema(type=types.Type.STRING),
                        "minutes": types.Schema(type=types.Type.INTEGER),
                    },
                    required=["label", "minutes"],
                ),
            ),
            "padded": types.Schema(
                type=types.Type.BOOLEAN,
                description="True if block_min is larger than the core activity duration alone "
                            "(i.e. pad_before_min or pad_after_min is nonzero)"),
        },
        required=["block_min", "default_duration_min", "pad_before_min", "pad_after_min",
                  "breakdown", "padded"],
    ),
)
_INFER_TOOL = types.Tool(function_declarations=[_INFER_BLOCK])

_VALID_TOD = (None, "morning", "afternoon", "evening")


def _infer_system_instruction() -> str:
    return (
        "You infer the TRUE time block an activity needs — including implicit prep, commute, "
        "and cleanup time a reasonable person would actually need, even if they didn't say it "
        "(e.g. a gym session needs commute there and back, and usually a shower after).\n\n"
        "You have NO knowledge of the user's calendar. Do not reason about it, mention it, or "
        "assume anything about what else is scheduled — that is handled entirely elsewhere, by "
        "different code you never see. Reason ONLY about the activity itself.\n\n"
        "RULES:\n"
        "1. block_min is the TOTAL minutes to hold: pad_before_min + default_duration_min + "
        "pad_after_min should equal block_min.\n"
        "2. default_duration_min is the core activity length alone (the actual workout, the "
        "actual meal) — not the padded total.\n"
        "3. If a STORED PREFERENCE is given, it OVERRIDES your own defaults for any field it "
        "lists. An explicit pad_after_min of 0 means the user handles that themselves (e.g. "
        "showers at home) — do NOT re-add it anyway. An explicit default_duration_min means use "
        "exactly that number, not your own guess. Any field the stored preference does NOT "
        "mention still gets your own reasonable default.\n"
        "4. breakdown is an ordered list of {label, minutes} that sums to block_min.\n"
        "5. padded is true whenever pad_before_min or pad_after_min is nonzero.\n"
        "6. tod_pref is a natural time-of-day fit ONLY if one is obvious for the activity itself "
        "(e.g. 'evening' for an after-work gym session) — omit it if there's no clear fit. If the "
        "user stated their own time-of-day preference in the request text, that always wins.\n"
        "7. Output ONLY the structured fields via the function call — no prose, no explanation, "
        "and never mention or imply anything about a calendar."
    )


def _parse_duration_fallback(stated_dur: str | None) -> int:
    """Best-effort minutes extraction from free text — used ONLY on the
    model-failure fallback path below; the normal path always gets its
    numbers from the model's structured output, never from parsing user
    text directly. Takes the upper end of a stated range ('1-1.5h' -> 90)
    so a fallback under-promises padding rather than over-promising it."""
    if not stated_dur:
        return 60
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", stated_dur)]
    if not nums:
        return 60
    value = max(nums)
    if re.search(r"h(ou)?r?s?\b", stated_dur, re.IGNORECASE):
        return round(value * 60)
    return round(value)


def _fallback_block(activity: str, stated_dur: str | None) -> dict:
    """The conservative failure mode for infer_block: an UNPADDED block
    (pad_before_min=pad_after_min=0). Under-promising is the safe
    direction to fail in — inventing 40 minutes of imagined commute time
    out of a failed model call would be worse than just using exactly
    what the user stated (or a 60-minute default)."""
    minutes = _parse_duration_fallback(stated_dur)
    return {
        "block_min": minutes,
        "default_duration_min": minutes,
        "pad_before_min": 0,
        "pad_after_min": 0,
        "tod_pref": None,
        "breakdown": [{"label": activity or "activity", "minutes": minutes}],
        "padded": False,
    }


def _normalize_block(args: dict, fallback: dict) -> dict:
    """Coerces the model's structured output into the exact shape callers
    (the placer, the proposal renderer) rely on — a malformed or missing
    field degrades to a sane default field-by-field rather than
    propagating a bad value into place_slots, which trusts block_min
    completely."""
    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    block_min = _int(args.get("block_min"), fallback["block_min"])
    if block_min <= 0:
        block_min = fallback["block_min"]
    default_duration_min = _int(args.get("default_duration_min"), fallback["default_duration_min"])
    pad_before_min = max(0, _int(args.get("pad_before_min"), 0))
    pad_after_min = max(0, _int(args.get("pad_after_min"), 0))

    tod_pref = args.get("tod_pref") or None
    if tod_pref not in _VALID_TOD:
        tod_pref = None

    breakdown = []
    for item in (args.get("breakdown") or []):
        try:
            breakdown.append({"label": str(item["label"]), "minutes": int(item["minutes"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not breakdown:
        breakdown = fallback["breakdown"]

    padded = bool(args.get("padded", pad_before_min > 0 or pad_after_min > 0))

    return {
        "block_min": block_min,
        "default_duration_min": default_duration_min,
        "pad_before_min": pad_before_min,
        "pad_after_min": pad_after_min,
        "tod_pref": tod_pref,
        "breakdown": breakdown,
        "padded": padded,
    }


def _stored_pref_prompt_lines(stored_pref: dict | None) -> str:
    """Builds the STORED PREFERENCE line from numeric fields ONLY. Never
    reads `stored_pref.get("notes")` here or anywhere else in this module
    — see THE notes WALL in core/db.py and the scheduling_preferences
    migration. notes is breakdown-phrasing copy for the PROPOSAL renderer
    (Part D, not built yet), never model input."""
    if not stored_pref:
        return ""
    parts = []
    if stored_pref.get("default_duration_min") is not None:
        parts.append(f"default_duration_min={stored_pref['default_duration_min']}")
    if stored_pref.get("pad_before_min") is not None:
        parts.append(f"pad_before_min={stored_pref['pad_before_min']}")
    if stored_pref.get("pad_after_min") is not None:
        parts.append(f"pad_after_min={stored_pref['pad_after_min']}")
    if stored_pref.get("tod_pref"):
        parts.append(f"tod_pref={stored_pref['tod_pref']}")
    if not parts:
        return ""
    return "STORED PREFERENCE (overrides your defaults for any field listed here): " + ", ".join(parts)


async def infer_block(activity: str, stated_dur: str | None, stored_pref: dict | None) -> dict:
    """Calendar-blind activity inference (Part B). The ONLY things this
    function ever sees are the activity name, what the user said about
    duration, and a stored preference row (numeric fields only — see THE
    notes WALL) — it NEVER sees or is passed the calendar, so it is
    structurally incapable of confabulating a schedule (THE INVARIANT,
    module docstring).

    Returns {block_min, default_duration_min, pad_before_min,
    pad_after_min, tod_pref, breakdown, padded}. Never raises — any model
    failure (timeout, no function call, malformed args) falls back to a
    conservative UNPADDED interpretation of `stated_dur` rather than
    guessing padding out of nowhere.
    """
    activity = (activity or "").strip()
    fallback = _fallback_block(activity, stated_dur)

    user_text = f"Activity: {activity!r}. Stated duration: {stated_dur or '(not given)'}."
    pref_line = _stored_pref_prompt_lines(stored_pref)
    if pref_line:
        user_text += f"\n{pref_line}"

    try:
        resp = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=_SCHEDULER_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_text)])],
                config=types.GenerateContentConfig(
                    system_instruction=_infer_system_instruction(),
                    tools=[_INFER_TOOL],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.ANY,
                            allowed_function_names=["infer_block"],
                        )
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    temperature=0,
                    safety_settings=SAFETY_SETTINGS,
                ),
            ),
            timeout=_INFER_TIMEOUT,
        )
        fcs = resp.function_calls
        if not fcs:
            log.warning("[scheduler] infer_block got no function call for %r — using fallback", activity)
            return fallback
        block = _normalize_block(dict(fcs[0].args), fallback)
        log.info("[scheduler] inferred block for %r: %s", activity, block)
        return block
    except Exception as exc:
        log.error("[scheduler] infer_block failed for %r: %s — using fallback", activity, exc)
        return fallback


# ---------------------------------------------------------------------------
# Part C — place_slots (pure, deterministic, no model/network — the safety
# core). Guarantee: every returned slot is drawn ONLY from a free_intervals()
# gap, so none can ever overlap a busy interval passed in, no matter what
# infer_block or anything upstream said. Heuristics (which gap/day is
# preferred, how ties are spread) are tunable; that one property is not.
# ---------------------------------------------------------------------------

_TOD_BANDS = {"morning": (8, 12), "afternoon": (12, 17), "evening": (17, 22)}
_DEFAULT_SPACING = {"max_consecutive_days": 3}   # avoid 3+ consecutive days


def clamp_to_hours(
    day: date, hours: tuple[int, int], tod_pref: str | None
) -> tuple[datetime, datetime] | None:
    """Daily bounds for `day`, narrowed to tod_pref's band (if any) and
    intersected with `hours`. Returns None if the intersection is empty
    (e.g. hours=(8,10) with tod_pref='evening') — that day contributes no
    slots rather than silently ignoring the constraint that produced the
    conflict."""
    lo, hi = hours
    if tod_pref in _TOD_BANDS:
        band_lo, band_hi = _TOD_BANDS[tod_pref]
        lo, hi = max(lo, band_lo), min(hi, band_hi)
    if lo >= hi:
        return None
    return datetime.combine(day, time(hour=lo)), datetime.combine(day, time(hour=hi))


def free_intervals(
    bounds: tuple[datetime, datetime], busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Classic gap sweep: sort busy intervals, walk them, emit the gaps
    between/around them within bounds. `busy` is every event overlapping
    `bounds` — EVERY event is an immovable wall, not just ones that look
    relevant to the activity."""
    start, end = bounds
    cursor = start
    gaps = []
    for b_start, b_end in sorted(busy, key=lambda e: e[0]):
        if b_start > cursor:
            gaps.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def align_block(gap: tuple[datetime, datetime], block_min: int, tod_pref: str | None) -> dict:
    """Places block_min inside a gap already known to be long enough.
    Evening preference aligns to the LATEST possible start (evening-most);
    everything else aligns to the EARLIEST possible start."""
    g_start, g_end = gap
    block = timedelta(minutes=block_min)
    if tod_pref == "evening":
        start = max(g_start, g_end - block)
    else:
        start = g_start
    return {"start": start, "end": start + block}


def _consecutive_run_length(days: set[date]) -> int:
    ordered = sorted(days)
    longest = run = 1 if ordered else 0
    for i in range(1, len(ordered)):
        if (ordered[i] - ordered[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return longest


def violates_spacing(slot: dict, chosen: list[dict], spacing: dict | None) -> bool:
    """True if adding `slot` would create a run of `max_consecutive_days`
    (or more) same-activity days in a row. v1 places at most one slot per
    day, so this only ever needs to reason about DAYS, not overlapping
    times within a day."""
    if not spacing:
        return False
    max_run = spacing.get("max_consecutive_days")
    if not max_run:
        return False
    days = {c["start"].date() for c in chosen} | {slot["start"].date()}
    return _consecutive_run_length(days) >= max_run


def select_count(chosen: list[dict], count: int, spacing: dict | None) -> list[dict]:
    """Picks exactly `count` slots from `chosen`, preferring an even
    spread across the window rather than just the earliest `count` —
    bunching everything at the front of the week when more days were
    available is a worse plan than spreading it out, even though both are
    "valid" (every candidate in `chosen` is already conflict-free)."""
    ordered = sorted(chosen, key=lambda s: s["start"])
    n = len(ordered)
    if count <= 0 or n == 0:
        return []
    if n <= count:
        return ordered
    step = n / count
    indices = sorted({min(n - 1, int(i * step)) for i in range(count)})
    i = 0
    while len(indices) < count:
        if i not in indices:
            indices.append(i)
            indices = sorted(set(indices))
        i += 1
    return [ordered[i] for i in indices[:count]]


def place_slots(
    busy: list[dict],
    block_min: int,
    count: int,
    window: list[date],
    tod_pref: str | None = None,
    spacing: dict | None = None,
    hours: tuple[int, int] = (8, 22),
    now: datetime | None = None,
) -> list[dict]:
    """The deterministic placer (Part C, the safety core).

    `busy` is every event in the window — a list of {"start": datetime,
    "end": datetime} — from a caller's live list_events() read. EVERY
    entry is treated as an immovable wall regardless of what it is; this
    function has no concept of "movable" or "unimportant" events.

    Returns up to `count` non-overlapping {"start","end"} dicts, each one
    drawn ONLY from a computed free gap — this is the structural guarantee
    that makes a double-book impossible here no matter what upstream
    reasoning produced `block_min`/`tod_pref`/etc. If fewer than `count`
    fit in `window`, returns however many DID fit — never invents slots to
    make up the difference.
    """
    if now is None:
        now = datetime.now()
    if spacing is None:
        spacing = _DEFAULT_SPACING

    chosen: list[dict] = []
    for day in window:
        if day < now.date():
            continue   # day fully in the past

        bounds = clamp_to_hours(day, hours, tod_pref)
        if bounds is None:
            continue
        b_start, b_end = bounds
        if day == now.date():
            b_start = max(b_start, now)   # never propose a slot earlier than right now
        if b_start >= b_end:
            continue

        busy_today = [
            (e["start"], e["end"]) for e in busy
            if e["start"] < b_end and e["end"] > b_start
        ]
        gaps = free_intervals((b_start, b_end), busy_today)

        for gap in gaps:
            if (gap[1] - gap[0]) >= timedelta(minutes=block_min):
                slot = align_block(gap, block_min, tod_pref)
                if not violates_spacing(slot, chosen, spacing):
                    chosen.append(slot)
                    break   # at most one slot per day (v1) -> naturally spreads across the window

    return select_count(chosen, count, spacing)
