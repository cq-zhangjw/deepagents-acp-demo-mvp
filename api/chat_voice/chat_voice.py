"""AI speech synthesis API (sub-router).

Engines (selectable via TTS_ENGINE):
  - kokoro   (default) Kokoro-82M local inference, ONNX Runtime CPU only,
             models under repo-root models/, in-memory WAV, no file written.
  - edge_tts Microsoft Edge online TTS, MP3 -> 24kHz PCM via PyAV; no local
             model, but requires access to speech.platform.bing.com
             (EDGE_TTS_PROXY can be set for restricted networks).

- Speech input (STT): browser Web Speech Recognition (webkitSpeechRecognition),
  done on the frontend; recognized text is filled into the main chat input.
- Speech output (TTS): this module calls the selected engine and synthesizes
  WAV/PCM in memory (no file written).
- Voice resolution: when the requested voice is missing/unknown (e.g. legacy
  Audio8 names zh/en), the language default voice configured via
  TTS_VOICE_ZH / TTS_VOICE_JA / TTS_VOICE_EN (kokoro) or EDGE_TTS_VOICE_ZH /
  EDGE_TTS_VOICE_JA / EDGE_TTS_VOICE_EN (edge_tts) is picked by text language.

Entry: app.py include_router (prefix /api/chat_voice)
"""

import base64
import logging
import os

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import tts_api
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat_voice", tags=["chat_voice"])

# voice name -> frontend display label (kokoro preset voices; edge voices carry their own label)
_VOICE_LABELS = {}

# playback mode: TTS_MODE=stream (streaming, low first-packet latency) | file (non-streaming, play after full WAV)
# frames per stream chunk: TTS_STREAM_CHUNK_FRAMES (sentence-level streaming; kept for compatibility only)
_SAMPLE_RATE = 24000


def _engine() -> str:
    """Active TTS engine: kokoro (default) | edge_tts."""
    engine = os.getenv("TTS_ENGINE", "kokoro").strip().lower()
    if engine in ("edge", "edge_tts", "edge-tts"):
        return "edge_tts"
    return "kokoro"


def _engine_module():
    if _engine() == "edge_tts":
        from . import edge_tts as mod
        return mod
    return tts_api


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
    """Map the active engine's voices to the frontend {label, value} format."""
    try:
        items = _engine_module().list_voices()
    except Exception as exc:  # noqa: BLE001
        logger.error("list voices failed: %s", exc)
        return []
    result = []
    for item in items:
        name = item.get("name", "")
        if not name:
            continue
        label = item.get("label") or _VOICE_LABELS.get(name, name)
        result.append({"label": label, "value": name})
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
        "engine": _engine(),
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
    engine = _engine()
    mod = _engine_module()

    def generate():
        if not req.text.strip():
            return
        try:
            voice = mod.resolve_voice(req.text, req.voice)
            for seg in tts_api.split_text(req.text):
                for _seq, pcm in mod.iter_pcm_chunks(seg, voice=voice):
                    yield pcm
        except Exception as exc:  # noqa: BLE001
            logger.error("%s tts stream failed: %s", engine, exc)

    return StreamingResponse(generate(), media_type="application/octet-stream")


def _synthesize(text: str, voice: str) -> bytes:
    if not text:
        return b""
    engine = _engine()
    try:
        voice = _engine_module().resolve_voice(text, voice)
        wav_bytes, _sample_rate = _engine_module().synthesize_wav_bytes(text, voice=voice)
        return wav_bytes
    except Exception as exc:  # noqa: BLE001
        logger.error("%s tts synthesis failed: %s", engine, exc)
        return b""
