import asyncio
import logging
import base64
import os
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from supabase import create_client, Client as SupabaseClient
from google.genai import types
from core.gemini import gemini_client, GEMINI_MODEL, generate_reply
from core.memory import get_session
from core.prompts import build_dynamic_prompt

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase auth (anon key — only used to verify JWTs, not for DB access)
# ---------------------------------------------------------------------------

_supabase: SupabaseClient | None = None
_bearer = HTTPBearer()


def _get_supabase() -> SupabaseClient:
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_ANON_KEY"],
        )
    return _supabase


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """Verify the Supabase JWT and return the user_id (UUID string).

    user_id comes exclusively from the verified token — never from the
    request body — so a client cannot impersonate another user.
    """
    try:
        result = await asyncio.to_thread(
            _get_supabase().auth.get_user, credentials.credentials
        )
        return result.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

VISION_MODEL = "gemini-2.5-flash"

# Process-wide flag: flipped to False on first multimodal failure so we
# silently fall back to the description path without ever retrying that way.
_direct_vision_ok = True

app = FastAPI(title="Pistachio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    username:   str
    message:    str
    screenshot: str | None = None


class ChatResponse(BaseModel):
    reply:        str
    working_memory: dict


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
        await session.compress_rolling(gemini_client, GEMINI_MODEL)

        try:
            reply = await asyncio.wait_for(
                generate_reply(session.contents, build_dynamic_prompt(session)),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            # Remove the message we just appended — it never got a response.
            session._contents.pop()
            session._db_ids.pop()
            raise HTTPException(status_code=504, detail="Gemini timed out.")
        except Exception as exc:
            # First multimodal failure: switch to description mode and retry once.
            if req.screenshot and _direct_vision_ok:
                log.warning("Direct vision rejected (%s) — switching to description fallback.", exc)
                _direct_vision_ok = False
                session._contents.pop()
                session._db_ids.pop()

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
                    session._contents.pop()
                    session._db_ids.pop()
                    log.error("Retry failed: %s", retry_exc)
                    raise HTTPException(status_code=500, detail=str(retry_exc))
            else:
                session._contents.pop()
                session._db_ids.pop()
                log.error("Generation error: %s", exc)
                raise HTTPException(status_code=500, detail=str(exc))

        if not reply:
            raise HTTPException(status_code=500, detail="No reply generated.")

        await session.append("assistant", reply)
        asyncio.create_task(session.update_working_memory(gemini_client, GEMINI_MODEL))

        return ChatResponse(
            reply=reply,
            working_memory={
                "location": session.working_memory.location,
                "activity": session.working_memory.activity,
                "mood":     session.working_memory.mood,
            },
        )


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
        await session.flush(gemini_client, GEMINI_MODEL)
    return {"status": "memory saved"}
