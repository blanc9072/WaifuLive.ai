PROJECT: WaifuLive.ai — agentic Live2D AI companion (desktop app)

What it is: An always-on-screen anime companion that lives on the user's desktop as a Live2D avatar, responds to text and voice, can watch the screen, and remembers things per-user across sessions. Brain is a fine-tuned Gemini 2.5 Flash endpoint on Vertex AI. Target market is lonely/isolated users; positioning leans companionship — NOT clinical therapy (see safety notes). Distribution is a downloadable app from a website, not app stores.

# WaifuLive — Tech Stack & Decision Record

An always-on desktop AI companion: a Live2D anime avatar that lives on screen, talks by text
and voice, watches the screen (opt-in), remembers per user, and acts as an agentic secretary
(calendar management, with more to come).

This document lists each significant tool/technology in the stack — **what it is, why we chose
it, how it's used here** — ordered from narrowest single-purpose tool up to the most central
platform pieces. A cross-cutting **Architecture Decisions** section at the end captures the
design invariants that span multiple tools.

Runtime shape: a Python backend (FastAPI HTTP API + a Gemini Live WebSocket relay, both in one
process) and an Electron desktop app, with Supabase for identity and persistence. Backend runs
on the user's Mac (several integrations are macOS-local).

---

## 1. EventKit (via PyObjC) — calendar reads

**What it is.** Apple's native calendar/reminders framework, accessed from Python through
`pyobjc-framework-EventKit`. It's the same engine Calendar.app itself uses.

**Why.** The calendar read path originally used AppleScript (`osascript`), which has a fatal
limitation: its `every event whose start date ≥ X` query tests the *master recurrence rule's*
literal start date, never the expanded occurrences. Any recurring event whose series began
before the query window returns nothing — so a schedule of weekly classes, dining, etc. was
effectively invisible. This was masked for the entire early build because every test used
one-off events. EventKit's `predicateForEvents(withStart:end:calendars:)` natively expands
recurring events into per-occurrence instances (including individually modified and excluded
occurrences), which is exactly what a schedule reader needs. It is also ~1000× faster than the
osascript path (single-digit ms vs. tens of seconds — see the cache note in Decisions).

**How.** `core/calendar_read_ek.py` exposes `permission_status()`, `enumerate_calendars()`, and
`read_window(start, end, writable_only)`, returning per-occurrence event dicts (title, occurrence
start/end, all-day, calendar, identifiers) in local time. It backs the display read ("what's on
my calendar"), the scheduler's busy-set, and the scheduler's pre-create conflict re-verify.
Requires the macOS **Calendars** TCC permission (distinct from the Automation permission
osascript uses); a permission miss returns an explicit `needs_permission` status, never a silent
empty result.

---

## 2. AppleScript / `osascript` — calendar writes

**What it is.** macOS's scripting bridge, invoked as a subprocess, driving Calendar.app to
create/move/delete events.

**Why.** Writes need to actually land in the user's real calendar (which syncs to Google/iCloud
via CalDAV through Calendar.app). osascript writes were spiked extensively and hardened, so
they're kept for writes even though reads moved to EventKit — reads and writes hit the same
backing store, so the split is safe, and there was no reason to re-risk proven write code.

**How.** `core/calendar_executor.py` builds AppleScript for create/move/delete and runs it off
the event loop with timeouts. Hard-won details baked in: dates are set field-by-field (AppleScript
string-parsing is locale-dependent and broke across machines); move uses order-dependent
start/end assignment to avoid the `-10025` "start must precede end" error on forward moves;
recurring events are **refused** for move/delete (deleting a recurring master silently no-ops;
moving one silently shifts the whole series); the OS-permission-denied signature (`-1743`) is
detected and surfaced cleanly. Events are targeted by iCal `uid` for delete/move.

---

## 3. faster-whisper — on-device speech-to-text

**What it is.** A fast, quantized reimplementation of OpenAI Whisper. We use the `base.en` model
at int8.

**Why.** Dictation (the mic-to-text button, separate from live voice mode) should be private and
free of per-request cost/latency to a cloud STT. On-device transcription keeps audio local and is
fast enough for dictation bursts on CPU.

**How.** `/transcribe` receives base64 WAV, runs `WhisperModel("base.en", device="cpu",
compute_type="int8")` in a worker thread, returns the transcript. The model (~154 MB) downloads
on first use and is cached thereafter.

---

## 4. ElevenLabs — text-to-speech

**What it is.** A cloud neural TTS API. We use the `eleven_flash_v2_5` model for low latency.

**Why.** The companion needs a natural, expressive voice, and per-user voice selection. ElevenLabs
gives high-quality voices addressable by ID, which maps cleanly onto per-user voice preferences.
It sits behind an abstraction so the provider can be swapped without touching the chat flow.

**How.** `core/tts.py` defines an abstract `Synthesizer` with an `ElevenLabsSynthesizer`
implementation (streaming `mp3_44100_128` over `httpx`), selected by the `TTS_ENGINE` env var.
Each user's `voices.tts_ref` (an ElevenLabs voice ID) is stored in Supabase and looked up per
reply. `clean_for_tts()` strips emoticons/markup/emoji from what's spoken without altering the
displayed text. TTS is non-fatal: a synthesis failure just omits audio.

---

## 5. Live2D (PixiJS + pixi-live2d-display + Cubism SDK) — the avatar

**What it is.** Live2D is 2D character rigging; the Cubism runtime animates the model, rendered in
the browser via PixiJS with the `pixi-live2d-display` binding.

**Why.** The avatar *is* the product's face — an expressive on-screen presence, not a chat window.
Live2D is the standard for this kind of anime-style rigged character and runs in Electron's
renderer with no native dependencies.

**How.** `model.html` runs a transparent, always-on-top, click-through Electron window holding the
PixiJS canvas. A persistent ticker drives idle motion (breathing, body sway), cursor-following
eye/head tracking, and audio-reactive mouth movement (lip-sync from a Web Audio analyser on the
TTS stream). Expressions are driven by an emotion classifier on replies and a mood value from
working memory, mapped through a per-avatar `expression_map` (stored in Supabase) so different
model files can reuse the same emotion keys. Models hot-swap when the user changes avatar.

---

## 6. Gemini Live API — real-time voice conversation

**What it is.** Google's low-latency bidirectional voice model
(`gemini-live-2.5-flash-native-audio`), spoken to over a WebSocket.

**Why.** Live voice mode (push-to-talk) needs true real-time streaming audio in and native audio
out with barge-in and transcription — a different modality than the text chat path. The Live API
is purpose-built for it.

**How.** `main.py` runs a WebSocket relay on `:8765`. The Electron client streams PCM mic audio
up; the relay forwards to Gemini Live and streams audio back down, plus input/output
transcriptions that render as chat bubbles. Per-turn transcripts are buffered and flushed into the
same memory system the text path uses, so voice and text share one continuous memory. (Note: the
voice model runs in `us-west1`; the text brain moved to `global` — see below.)

---

## 7. Electron — desktop application shell

**What it is.** Chromium + Node.js for building a cross-platform desktop app from web tech.

**Why.** The product must live *on the desktop* — a floating always-on-top avatar, global
hotkeys, screen capture, system-tray control, idle detection. A browser tab can't do those;
Electron can, while letting the UI be plain HTML/CSS/JS.

**How.** `main.js` (main process) owns auth, session/token storage, and acts as an API proxy that
injects the user's bearer token before forwarding to the backend — so **secrets never reach the
renderer**. `preload.js` exposes a narrow `contextBridge` IPC surface. Renderer windows: login,
chat (text + mic + dictation + settings popup), model (the avatar), settings. Uses
`desktopCapturer` for opt-in screen-watch, `powerMonitor` for idle-based proactive nudges, and a
system `Tray` for screen-watch / Do-Not-Disturb toggles.

---

## 8. Supabase — identity & persistence

**What it is.** Managed Postgres with built-in auth, row-level security, and a CLI for migrations.

**Why.** The app needs real per-user accounts and durable per-user state (persona, voice, avatar,
memory, permissions) with hard isolation between users. Supabase gives auth + Postgres + RLS in
one place, and the client can hit it directly with the user's token for settings reads/writes
(RLS enforces ownership), keeping that off the backend's critical path.

**How.** Auth issues JWTs the backend verifies. Tables: `profiles` (per-user config +
calendar-permission flags), `personas`/`voices`/`avatars` (lookup tables), `messages`,
`long_term_memory`, `working_memory`, `scheduling_preferences`. **RLS is owner-only** on all
user-scoped tables (`auth.uid() = user_id`), so a user structurally cannot read or write another's
rows. A `handle_new_user` trigger auto-provisions a profile pointing at default persona/voice/
avatar. Schema changes go through versioned CLI migrations (`supabase db push` to the hosted DB).

---

## 9. FastAPI + Uvicorn (+ websockets) — the backend spine

**What it is.** An async Python web framework (FastAPI) on the Uvicorn ASGI server, plus the
`websockets` library for the voice relay.

**Why.** The backend is glue between many async I/O sources (Gemini, Supabase, ElevenLabs,
Whisper, the calendar bridge). FastAPI's async model fits, Pydantic gives typed request/response
contracts, and dependency injection makes token-verified auth clean. Running the HTTP API and the
Live voice relay as two servers in one asyncio process keeps deployment simple.

**How.** `main.py` runs the FastAPI app (`:8000`) and the Gemini Live WebSocket relay (`:8765`)
concurrently via `asyncio.gather`. HTTP endpoints: `/chat`, `/nudge`, `/transcribe`, `/profile`,
`/memory`, `/sleep`, and the calendar action endpoints (`/calendar/...`). Auth is a FastAPI
dependency that verifies the Supabase JWT and derives `user_id` from the token only (never the
request body), so a client can't impersonate another user.

---

## 10. Google Gemini on Vertex AI (`google-genai`) — the brain

**What it is.** Google's Gemini model family, accessed through the `google-genai` SDK against
Vertex AI. This is the core intelligence — persona, conversation, and the tool-use pipeline.

**Why.** The companion's personality, memory synthesis, screen understanding, and agentic
tool-calling all run on Gemini. It's the reason the product exists, and everything else is built
around feeding it context and executing what it decides.

**How — the models in play:**
- **`gemini-3.6-flash`** — the chat brain. Runs at `location="global"` (the Gemini 3.x family
  isn't served in `us-west1`), `thinking_level=MINIMAL` (a texting companion wants speed and short
  replies, not visible reasoning), temperature `1.0`. Model + location + thinking are config-driven
  so the previous **fine-tuned 2.5 endpoint** (in `us-west1`) remains a one-env-var rollback.
- **`gemini-2.5-flash`** — the utility model: Stage-2 tool resolver (plain-English → typed function
  call, temperature 0), working-memory extraction, and the screen-vision fallback.
- **`gemini-live-2.5-flash-native-audio`** — live voice (see §6).

Auth is via Vertex AI + Application Default Credentials from a service-account file
(`google-key.json`), not an API key. The persona lives entirely in the system prompt (not the
weights), which is what made swapping the fine-tune for a stock model low-risk.

---

## Architecture Decisions (cross-cutting)

**Two-stage tool pipeline (signal → resolve → execute).** The chat model emits a plain-English
`<action>` signal; a separate resolver model turns it into a typed call; an executor runs it. This
split originally existed because grounding (web search) and function-calling were mutually
exclusive on the fine-tuned endpoint. On `gemini-3.6-flash` they now coexist, so the split is no
longer forced — but it's kept because each stage is independently testable and the executor stays
model-agnostic (reusable by the voice path later).

**Model reasons about intent; code guarantees safety.** The scheduler is the clearest example: a
*calendar-blind* model step infers what an activity really needs (e.g. "gym" = workout + commute +
shower ≈ 2.5h), then a *deterministic* placer finds real free gaps against a live read and can
only ever select from computed-free time. The model never sees the calendar; the placer never
guesses. This makes double-booking structurally impossible regardless of model behavior.

**The phantom fence.** Because the chat model generates its reply *before* tools run, it could
fabricate a schedule from memory. For read-type actions, its pre-tool text is forced to a
content-free acknowledgment, and a client-side + TTS-side backstop suppresses any fabricated
schedule from ever reaching the user on screen or in audio — so a real read is the only source of
calendar facts.

**Per-action, default-deny permissions.** Calendar access is a master switch plus per-action
grants (create/list/move/delete) on `profiles`, all defaulting to false, enforced server-side and
gated by RLS. Users control exactly what the assistant may do — a core product stance
("customizable, hard-restricted agency"), not just security hygiene.

**Destructive/durable actions confirm; approximate never auto-acts.** Delete/move act only on an
unambiguous exact match (else they ask "which one?"); typo-tolerant matching that requires
guessing must be confirmed before acting; learned scheduling preferences are echoed and only
persisted on user acceptance, and are always inspectable/resettable.

**EventKit reads / osascript writes.** Reads moved to EventKit (recurrence expansion + speed);
writes stay on the hardened osascript path. They share Calendar.app's backing store, and
delete/move stay fully osascript so their read→identify→write stays on one identifier system.
(Future: moving writes to EventKit would unlock proper recurring delete/move via EventKit's
`span: thisEvent/futureEvents` semantics.)

**The cache that came and went.** When reads were slow osascript (a serialized ~40–84s
multi-calendar race), a Supabase snapshot cache with a background refresher was built to move
reads off the request path. EventKit then made live reads ~3–92 ms — faster than reading the cache
would be — so the entire cache was retired. Kept as a lesson: a workaround earns its place against
the constraint of its time and should be removed, not preserved, once the real fix lands.

---

## Known gaps / roadmap

- **Write targeting:** new events currently land on the first writable calendar (iCloud Home), not
  the user's primary Google calendar. A default-calendar preference + per-request override is the
  top remaining calendar item for a Google-primary user.
- **Reminders + Do-Not-Disturb granularity:** proactive calendar reminders (a live-EventKit
  periodic task) with DnD sub-toggles (nudges vs. reminders).
- **Recurring create / management:** now feasible — reads see recurring events, and EventKit
  writes could lift the current recurring delete/move refusal.
