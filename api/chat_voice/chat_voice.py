"""AI speech synthesis API (sub-router).

Engines (selectable via TTS_ENGINE):
  - kokoro   (default) Kokoro-82M local inference, ONNX Runtime CPU only,
             models under repo-root models/, in-memory WAV, no file written.
  - edge_tts Microsoft Edge online TTS, MP3 -> 24kHz PCM via PyAV; no local
             model, but requires access to speech.platform.bing.com
             (EDGE_TTS_PROXY can be set for restricted networks).
  - sapi     Windows built-in speech voices (SAPI via win32com), 24kHz mono;
             zero dependencies beyond the OS, always available on Windows.

Fallback (TTS_FALLBACK=true by default): when the primary engine fails (e.g.
Edge service unreachable), speech falls back to the Windows SAPI voices so
voice features never go silent.

- Speech input (STT): browser Web Speech Recognition (webkitSpeechRecognition),
  done on the frontend; recognized text is filled into the main chat input.
- Speech output (TTS): this module calls the selected engine and synthesizes
  WAV/PCM in memory (no file written).
- Voice resolution: when the requested voice is missing/unknown (e.g. legacy
  Audio8 names zh/en), the language default voice configured via
  TTS_VOICE_ZH / TTS_VOICE_JA / TTS_VOICE_EN (kokoro), EDGE_TTS_VOICE_ZH /
  EDGE_TTS_VOICE_JA / EDGE_TTS_VOICE_EN (edge_tts) or SAPI_VOICE_ZH /
  SAPI_VOICE_JA / SAPI_VOICE_EN (sapi) is picked by text language.

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

# voice name -> frontend display label (kokoro preset voices; edge/sapi voices carry their own label)
_VOICE_LABELS = {}

# playback mode: TTS_MODE=stream (streaming, low first-packet latency) | file (non-streaming, play after full WAV)
# frames per stream chunk: TTS_STREAM_CHUNK_FRAMES (sentence-level streaming; kept for compatibility only)
_SAMPLE_RATE = 24000


def _engine() -> str:
    """Active TTS engine: kokoro (default) | edge_tts | sapi."""
    engine = os.getenv("TTS_ENGINE", "kokoro").strip().lower()
    if engine in ("edge", "edge_tts", "edge-tts"):
        return "edge_tts"
    if engine in ("sapi", "windows", "windows_sapi"):
        return "sapi"
    return "kokoro"


def _fallback_enabled() -> bool:
    """TTS_FALLBACK: when true, fall back to Windows SAPI on primary failure."""
    return os.getenv("TTS_FALLBACK", "true").strip().lower() in ("1", "true", "yes", "on")


def _engine_module():
    if _engine() == "edge_tts":
        from . import edge_tts as mod
        return mod
    if _engine() == "sapi":
        from . import sapi_tts as mod
        return mod
    return tts_api


def _fallback_module():
    """Windows SAPI fallback module; None when unavailable (non-Windows)."""
    try:
        from . import sapi_tts as mod
        return mod
    except Exception as exc:  # noqa: BLE001
        logger.error("sapi fallback unavailable: %s", exc)
        return None


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
        "fallback": _fallback_enabled(),
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
    a continuous PCM byte stream. On primary-engine failure the stream falls back
    to Windows SAPI (when TTS_FALLBACK=true) so playback still works.
    """
    engine = _engine()
    mod = _engine_module()

    def _stream(mod_):
        voice = mod_.resolve_voice(req.text, req.voice)
        for seg in tts_api.split_text(req.text):
            for _seq, pcm in mod_.iter_pcm_chunks(seg, voice=voice):
                yield pcm

    def generate():
        if not req.text.strip():
            return
        try:
            yield from _stream(mod)
        except Exception as exc:  # noqa: BLE001
            logger.error("%s tts stream failed: %s", engine, exc)
            fallback = _fallback_module() if _fallback_enabled() else None
            if fallback is not None:
                try:
                    yield from _stream(fallback)
                except Exception as exc2:  # noqa: BLE001
                    logger.error("sapi tts stream fallback failed: %s", exc2)
            else:
                logger.warning("no fallback available; tts stream empty")

    return StreamingResponse(generate(), media_type="application/octet-stream")


def _synthesize(text: str, voice: str) -> bytes:
    if not text:
        return b""
    engine = _engine()
    try:
        voice = _engine_module().resolve_voice(text, voice)
        wav_bytes, _sample_rate = _engine_module().synthesize_wav_bytes(text, voice=voice)
        if wav_bytes:
            return wav_bytes
    except Exception as exc:  # noqa: BLE001
        logger.error("%s tts synthesis failed: %s", engine, exc)
    # fallback: Windows SAPI so voice features keep working
    if _fallback_enabled():
        fallback = _fallback_module()
        if fallback is not None:
            try:
                voice = fallback.resolve_voice(text, voice)
                wav_bytes, _sample_rate = fallback.synthesize_wav_bytes(text, voice=voice)
                return wav_bytes
            except Exception as exc2:  # noqa: BLE001
                logger.error("sapi tts synthesis fallback failed: %s", exc2)
        else:
            logger.warning("fallback disabled/unavailable; tts empty")
    return b""
