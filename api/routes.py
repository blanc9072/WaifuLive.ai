import asyncio
import logging
import base64
import os
import re
import tempfile
from faster_whisper import WhisperModel
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from supabase import create_client, Client as SupabaseClient
from google.genai import types
from core.gemini import gemini_client, GEMINI_MODEL, generate_reply
from core.memory import get_session
from core.prompts import build_dynamic_prompt
from core.tool_resolver import resolve_calendar_action
from core.calendar_executor import create_calendar_event
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
                # Stage 3: actually execute resolved create_event calls. Never
                # raises — a clean "error" result on failure, /chat still returns.
                if resolved.status == "resolved" and resolved.tool == "create_event":
                    calendar_action = await create_calendar_event(resolved.args)
                    log.info("[stage3] execution result: %s", calendar_action)
            except Exception as exc:
                log.warning("[stage2/3] calendar tool pipeline failed (non-fatal): %s", exc)
        if not reply:
            reply = "on it!"

        await session.append("assistant", reply)
        asyncio.create_task(session.update_working_memory(gemini_client, MEMORY_MODEL))

        # TTS — non-fatal: missing voice or synthesis error just omits audio.
        audio_b64: str | None = None
        try:
            tts_ref = await db.fetch_voice_tts_ref(user_id)
            if tts_ref:
                audio_bytes = await tts.synthesize(reply, tts_ref)
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
