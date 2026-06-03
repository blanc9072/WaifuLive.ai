import asyncio
import logging
import base64
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google.genai import types
from core.gemini import gemini_client, GEMINI_MODEL, generate_reply
from core.memory import (
    chat_session, chat_lock,
    working_memory, long_term_memory,
    maybe_compress_rolling_memory,
    update_working_memory,
    flush_memory_to_disk,
)
from core.prompts import build_dynamic_prompt

log = logging.getLogger(__name__)

VISION_MODEL = "gemini-2.5-flash"

# Optimistic: assume fine-tuned endpoint accepts images (true for most Gemini
# 1.5/2.0-based fine-tunes). Flipped to False on first multimodal failure so
# we silently switch to the description fallback without ever retrying.
_direct_vision_ok = True

app = FastAPI(title="Pistachio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    username: str
    message: str
    screenshot: str | None = None


class ChatResponse(BaseModel):
    reply: str
    working_memory: dict


class MemoryResponse(BaseModel):
    long_term: str
    working: dict


async def describe_screen(image_bytes: bytes) -> str:
    """Fallback: use a standard vision model to turn the screenshot into text."""
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
    """Build Content parts list for the fine-tuned model."""
    text_part = types.Part.from_text(text=message_text)
    if image_bytes and use_direct:
        return [types.Part.from_bytes(data=image_bytes, mime_type="image/png"), text_part]
    return [text_part]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    global _direct_vision_ok
    async with chat_lock:
        message_text = f"[{req.username}]: {req.message}"
        image_bytes  = base64.b64decode(req.screenshot) if req.screenshot else None

        # If falling back to description, inject it as text now
        if image_bytes and not _direct_vision_ok:
            desc = await describe_screen(image_bytes)
            if desc:
                message_text += f"\n[Screen: {desc}]"
            image_bytes = None  # consumed; don't pass to model

        parts = _build_parts(message_text, image_bytes, _direct_vision_ok)
        chat_session.append(types.Content(role="user", parts=parts))
        await maybe_compress_rolling_memory(gemini_client, GEMINI_MODEL)

        try:
            reply = await asyncio.wait_for(
                generate_reply(list(chat_session), build_dynamic_prompt()),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            chat_session.pop()
            raise HTTPException(status_code=504, detail="Gemini timed out.")
        except Exception as exc:
            # First multimodal failure: switch to description mode and retry once
            if req.screenshot and _direct_vision_ok:
                log.warning("Direct vision rejected (%s) — switching to description fallback permanently.", exc)
                _direct_vision_ok = False
                chat_session.pop()

                desc = await describe_screen(base64.b64decode(req.screenshot))
                retry_text = f"[{req.username}]: {req.message}"
                if desc:
                    retry_text += f"\n[Screen: {desc}]"
                chat_session.append(types.Content(
                    role="user", parts=[types.Part.from_text(text=retry_text)]
                ))
                try:
                    reply = await asyncio.wait_for(
                        generate_reply(list(chat_session), build_dynamic_prompt()),
                        timeout=30.0,
                    )
                except Exception as retry_exc:
                    chat_session.pop()
                    log.error("Retry failed: %s", retry_exc)
                    raise HTTPException(status_code=500, detail=str(retry_exc))
            else:
                chat_session.pop()
                log.error("Generation error: %s", exc)
                raise HTTPException(status_code=500, detail=str(exc))

        if not reply:
            raise HTTPException(status_code=500, detail="No reply generated.")

        chat_session.append(
            types.Content(role="model", parts=[types.Part.from_text(text=reply)])
        )
        asyncio.create_task(update_working_memory(list(chat_session), gemini_client, GEMINI_MODEL))

        return ChatResponse(
            reply=reply,
            working_memory={
                "location": working_memory.location,
                "activity": working_memory.activity,
                "mood":     working_memory.mood,
            },
        )


@app.get("/memory", response_model=MemoryResponse)
async def get_memory():
    return MemoryResponse(
        long_term=long_term_memory,
        working={
            "location": working_memory.location,
            "activity": working_memory.activity,
            "mood":     working_memory.mood,
        },
    )


@app.post("/sleep")
async def sleep():
    await flush_memory_to_disk(gemini_client, GEMINI_MODEL)
    return {"status": "memory saved"}
