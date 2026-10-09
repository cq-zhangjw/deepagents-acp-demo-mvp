"""AI speech synthesis API (sub-router, Kokoro-82M edition).

- Speech input (STT): browser Web Speech Recognition (webkitSpeechRecognition),
  done on the frontend; recognized text is filled into the main chat input.
- Speech output (TTS): this module calls Kokoro-82M TTS (ONNX Runtime CPU only,
  models under the repo-root models/), synthesizes WAV in memory, no file written.
- Voice resolution: when the requested voice is missing/unknown (e.g. legacy
  Audio8 names zh/en), the language default voice configured via
  `TTS_VOICE_ZH` / `TTS_VOICE_JA` / `TTS_VOICE_EN` is picked by text language.

Entry: app.py include_router (prefix /api/chat_voice)
"""

import base64
import logging
import os

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .tts_api import iter_pcm_chunks, list_voices, resolve_voice, split_text, synthesize_wav_bytes
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat_voice", tags=["chat_voice"])

# voice name -> frontend display label (Kokoro preset voices: af_*/am_*/bf_*/bm_* en, 54 in v1.0)
_VOICE_LABELS = {}

# playback mode: TTS_MODE=stream (streaming, low first-packet latency) | file (non-streaming, play after full WAV)
# frames per stream chunk: TTS_STREAM_CHUNK_FRAMES (Kokoro is sentence-level streaming; kept for compatibility only)
_SAMPLE_RATE = 24000


def _tts_mode() -> str:
    mode = os.getenv("TTS_MODE", "file").strip().lower()
    return mode if mode in ("stream", "file") else "file"


def _stream_chunk_frames() -> int:
    try:
        value = int(os.getenv("TTS_STREAM_CHUNK_FRAMES", "48"))
        return max(6, value)
    except ValueError:
        return 48


def _voice_list() -> list[dict]:
    """Map downloaded Kokoro voices to the frontend {label, value} format."""
    try:
        items = list_voices()
    except Exception as exc:  # noqa: BLE001
        logger.error("list voices failed: %s", exc)
        return []
    result = []
    for item in items:
        name = item.get("name", "")
        if not name:
            continue
        result.append({
            "label": _VOICE_LABELS.get(name, name),
            "value": name,
        })
    return result


class TTSRequest(BaseModel):
    text: str = Field(..., description="text to read aloud")
    voice: str = Field(default="", description="TTS voice name; empty or unknown -> language default voice")


class TTSResponse(BaseModel):
    text: str = Field(..., description="echoed text")
    audio: str = Field(..., description="TTS WAV base64 (empty string on synthesis failure)")


@router.get("/voices")
def get_voices():
    """Return the list of registered voices."""
    return _voice_list()


@router.get("/config")
def get_config():
    """Return playback mode config; the frontend picks stream/file path accordingly."""
    return {
        "mode": _tts_mode(),
        "chunk_frames": _stream_chunk_frames(),
        "sample_rate": _SAMPLE_RATE,
    }


@router.post("/tts", response_model=TTSResponse)
def tts(req: TTSRequest):
    """Plain TTS: synthesize the given text into WAV base64 (in-memory, no file, no LLM)."""
    audio = _synthesize(req.text, req.voice)
    return TTSResponse(text=req.text, audio=base64.b64encode(audio).decode())


@router.post("/tts_stream")
def tts_stream(req: TTSRequest):
    """Streaming TTS: yield 16-bit PCM (mono 24kHz LE) segment by segment to lower first-packet latency.

    Long text is split into sentence segments and streamed; the frontend receives
    a continuous PCM byte stream; the stream ends early on failure.
    """
    def generate():
        if not req.text.strip():
            return
        try:
            voice = resolve_voice(req.text, req.voice)
            for seg in split_text(req.text):
                for _seq, pcm in iter_pcm_chunks(seg, voice=voice):
                    yield pcm
        except Exception as exc:  # noqa: BLE001
            logger.error("kokoro tts stream failed: %s", exc)

    return StreamingResponse(generate(), media_type="application/octet-stream")


def _synthesize(text: str, voice: str) -> bytes:
    if not text:
        return b""
    try:
        voice = resolve_voice(text, voice)
        wav_bytes, _sample_rate = synthesize_wav_bytes(text, voice=voice)
        return wav_bytes
    except Exception as exc:  # noqa: BLE001
        logger.error("kokoro tts synthesis failed: %s", exc)
        return b""
