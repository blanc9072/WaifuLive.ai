"""
TTS abstraction layer.

The chat flow calls the module-level synthesize() and never references any
concrete engine.  To add a new engine:
  1. Write a new class that inherits Synthesizer and implements synthesize().
  2. Add an elif branch in get_synthesizer().
  3. Set TTS_ENGINE=<your-key> in .env.
"""

import abc
import logging
import os

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class Synthesizer(abc.ABC):
    @abc.abstractmethod
    async def synthesize(self, text: str, voice_id: str) -> bytes:
        """Return audio bytes (MP3 or engine-native format) for the given text."""


# ---------------------------------------------------------------------------
# ElevenLabs implementation
# ---------------------------------------------------------------------------

class ElevenLabsSynthesizer(Synthesizer):
    _BASE  = "https://api.elevenlabs.io/v1"
    _MODEL = "eleven_flash_v2_5"

    def __init__(self) -> None:
        self._api_key = os.environ["ELEVENLABS_API_KEY"]

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        url     = f"{self._BASE}/text-to-speech/{voice_id}/stream"
        headers = {"xi-api-key": self._api_key, "Content-Type": "application/json"}
        payload = {
            "text":          text,
            "model_id":      self._MODEL,
            "output_format": "mp3_44100_128",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as r:
                r.raise_for_status()
                chunks: list[bytes] = []
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
        return b"".join(chunks)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_synthesizer: Synthesizer | None = None


def get_synthesizer() -> Synthesizer:
    global _synthesizer
    if _synthesizer is not None:
        return _synthesizer
    engine = os.getenv("TTS_ENGINE", "elevenlabs").lower()
    if engine == "elevenlabs":
        _synthesizer = ElevenLabsSynthesizer()
    else:
        raise RuntimeError(
            f"Unknown TTS_ENGINE={engine!r}.  "
            "Add an implementation in core/tts.py and register it here."
        )
    return _synthesizer


async def synthesize(text: str, voice_id: str) -> bytes:
    """Convenience wrapper — the only symbol the chat flow should import."""
    return await get_synthesizer().synthesize(text, voice_id)
