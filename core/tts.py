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
import re

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTS text sanitizer
# ---------------------------------------------------------------------------

_TTS_STRIP = [
    (r'</3', ''),               # broken heart (must come before <3)
    (r'<3', ''),                # heart emoticon -> silent
    (r'[:;=]-?[\)\(DPp]', ''), # basic smileys :) :( :D etc
    (r'\*[^*]+\*', ''),         # *actions* like *hugs* -> silent
    (r'[~^]', ''),              # stray tildes/carets
]


def _is_symbol(ch: str) -> bool:
    import unicodedata
    cat = unicodedata.category(ch)
    return cat.startswith('So') or cat.startswith('Sk')


def clean_for_tts(text: str) -> str:
    """Remove emoticons/markup/emoji that TTS would mispronounce.
    Does NOT touch the displayed text — only what gets synthesized."""
    for pat, rep in _TTS_STRIP:
        text = re.sub(pat, rep, text)
    text = ''.join(ch for ch in text if not _is_symbol(ch))
    return re.sub(r'\s{2,}', ' ', text).strip()


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
            log.info("[tts] sending %d chars: %r", len(text), text[:120])
            async with client.stream("POST", url, headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    body = await r.aread()
                    log.error("[tts] ElevenLabs %d: %s", r.status_code, body.decode(errors="replace")[:400])
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
    cleaned = clean_for_tts(text)
    log.info("[tts] clean_for_tts: in=%r -> out=%r", text[:80], cleaned[:80])
    if not cleaned:
        log.warning("[tts] cleaned text empty, skipping synthesis")
        return b''
    return await get_synthesizer().synthesize(cleaned, voice_id)
