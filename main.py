"""
Single entry point for the Pistachio backend.

Runs two servers concurrently in one asyncio process:
  • FastAPI HTTP API  — http://localhost:8000  (chat, memory, sleep)
  • Gemini Live relay — ws://localhost:8765    (voice WebSocket)
"""

import asyncio
import json
import logging
import os
import traceback

import uvicorn
import websockets
from dotenv import load_dotenv
from google import genai
from google.genai import types

from api.routes import app
from core.prompts import build_dynamic_prompt

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
for _noisy in ("httpx", "httpcore", "h2", "hpack", "urllib3", "google_genai",
               "google.genai", "websockets.client", "websockets.server"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)

API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# ---------------------------------------------------------------------------
# Gemini Live relay (voice WebSocket — ws://localhost:8765)
# ---------------------------------------------------------------------------

_LIVE_PROJECT   = "andrewgpt-490605"
_LIVE_LOCATION  = "us-west1"
_LIVE_MODEL     = "gemini-live-2.5-flash-native-audio"
_LIVE_SAMPLE_HZ = 16000

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google-key.json"
_live_client = genai.Client(vertexai=True, project=_LIVE_PROJECT, location=_LIVE_LOCATION)


def _live_session_config():
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=build_dynamic_prompt(),   # no user session — voice uses defaults
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def _handle_voice_client(ws) -> None:
    log.debug("[Voice] Client connected: %s", ws.remote_address)
    try:
        async with _live_client.aio.live.connect(
            model=_LIVE_MODEL, config=_live_session_config()
        ) as session:
            log.debug("[Voice] Gemini Live session opened.")

            async def recv_from_client():
                async for msg in ws:
                    if isinstance(msg, bytes):
                        await session.send_realtime_input(
                            audio=types.Blob(data=msg, mime_type=f"audio/pcm;rate={_LIVE_SAMPLE_HZ}")
                        )
                    elif isinstance(msg, str):
                        data = json.loads(msg)
                        if data.get("type") == "text":
                            await session.send_client_content(
                                turns=[types.Content(role="user", parts=[types.Part(text=data["text"])])],
                                turn_complete=True,
                            )

            async def recv_from_gemini():
                async for response in session.receive():
                    if response.data:
                        await ws.send(response.data)
                    if response.server_content:
                        sc = response.server_content
                        if sc.output_transcription and sc.output_transcription.text:
                            await ws.send(json.dumps({"type": "transcript",       "text": sc.output_transcription.text}))
                        if sc.input_transcription  and sc.input_transcription.text:
                            await ws.send(json.dumps({"type": "input_transcript", "text": sc.input_transcription.text}))

            await asyncio.gather(recv_from_client(), recv_from_gemini())

    except websockets.exceptions.ConnectionClosedOK:
        log.debug("[Voice] Client disconnected cleanly.")
    except Exception as exc:
        log.error("[Voice] Error: %s", exc)
        traceback.print_exc()


async def run_voice_relay() -> None:
    log.debug("Starting Gemini Live relay on ws://localhost:8765")
    async with websockets.serve(_handle_voice_client, "localhost", 8765):
        await asyncio.Future()   # run forever


# ---------------------------------------------------------------------------
# FastAPI HTTP server
# ---------------------------------------------------------------------------

async def run_api() -> None:
    config = uvicorn.Config(app, host=API_HOST, port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    log.debug("Starting Pistachio API on %s:%s", API_HOST, API_PORT)
    await server.serve()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    results = await asyncio.gather(
        run_api(),
        run_voice_relay(),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            log.error("Server task failed: %s", r)


if __name__ == "__main__":
    asyncio.run(main())
