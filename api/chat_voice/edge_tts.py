"""Microsoft Edge TTS (online) wrapper, drop-in compatible with tts_api.py.

Used when TTS_ENGINE=edge_tts. Speech is synthesized by the free Microsoft
Edge online service over a websocket; the returned MP3 stream is decoded to
24kHz mono 16-bit PCM with PyAV. No local model files are required, so this
engine has zero CPU cost (but needs internet access to speech.platform.bing.com;
in restricted networks set EDGE_TTS_PROXY to a working http(s) proxy).

Env controls (all optional):
  EDGE_TTS_VOICE_ZH / EDGE_TTS_VOICE_JA / EDGE_TTS_VOICE_EN  default voice per language
  EDGE_TTS_RATE    speech rate, e.g. "+0%"
  EDGE_TTS_VOLUME  e.g. "+0%"
  EDGE_TTS_PITCH   e.g. "+0Hz"
  EDGE_TTS_PROXY   http proxy for the Edge service, e.g. "http://127.0.0.1:7890"

Public API mirrors tts_api.py:
  synthesize_wav_bytes(text, voice) -> (WAV bytes, sample_rate)
  iter_pcm_chunks(text, voice)       -> yields (seq, 16-bit PCM bytes)
  list_voices()                      -> [{"name", "label", "locale"}]
  resolve_voice(text, voice)         -> actual voice name
  detect_language / split_text       -> re-exported from tts_api
"""

import asyncio
import io
import logging
import os
from pathlib import Path

import av
import edge_tts
import numpy as np
import soundfile as sf
from dotenv import load_dotenv

from .tts_api import detect_language, split_text

load_dotenv()  # ensure .env EDGE_TTS_* settings take effect

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000  # Edge service is 24kHz; decoded PCM is resampled to this

_DEFAULT_VOICES = {
    "zh": os.getenv("EDGE_TTS_VOICE_ZH", "zh-CN-XiaoxiaoNeural"),
    "ja": os.getenv("EDGE_TTS_VOICE_JA", "ja-JP-NanamiNeural"),
    "en": os.getenv("EDGE_TTS_VOICE_EN", "en-US-AriaNeural"),
}
_RATE = os.getenv("EDGE_TTS_RATE", "+0%")
_VOLUME = os.getenv("EDGE_TTS_VOLUME", "+0%")
_PITCH = os.getenv("EDGE_TTS_PITCH", "+0Hz")
_PROXY = os.getenv("EDGE_TTS_PROXY", "").strip() or None

_voice_cache: list[dict] | None = None


def resolve_voice(text: str, voice: str) -> str:
    """Resolve the requested voice: explicit choice wins, otherwise the
    language default for the detected text language."""
    voice = (voice or "").strip()
    if voice:
        return voice
    return _DEFAULT_VOICES.get(detect_language(text), _DEFAULT_VOICES["en"])


def list_voices() -> list[dict]:
    """Return the online Edge voice catalogue filtered to useful fields.

    Cached after the first successful fetch. On network failure returns an
    empty list (default voices per language still work via resolve_voice).
    """
    global _voice_cache
    if _voice_cache is not None:
        return _voice_cache
    try:
        raw = asyncio.run(edge_tts.list_voices())
    except Exception as exc:  # noqa: BLE001
        logger.error("edge-tts list_voices failed: %s", exc)
        return []
    items = [
        {
            "name": v.get("ShortName", ""),
            "label": v.get("FriendlyName") or v.get("ShortName", ""),
            "locale": v.get("Locale", ""),
        }
        for v in raw
        if v.get("ShortName")
    ]
    _voice_cache = items
    return items


def _decode_to_pcm16(mp3_bytes: bytes) -> np.ndarray:
    """Decode MP3 bytes into mono 16-bit PCM at SAMPLE_RATE (24kHz)."""
    if not mp3_bytes:
        raise RuntimeError("empty audio from edge-tts")
    container = av.open(io.BytesIO(mp3_bytes))
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    out: list[np.ndarray] = []
    for frame in container.decode(stream):
        for rframe in resampler.resample(frame):
            out.append(rframe.to_ndarray().reshape(-1))
    for rframe in resampler.resample(None):  # flush the resampler
        out.append(rframe.to_ndarray().reshape(-1))
    container.close()
    if not out:
        raise RuntimeError("no audio decoded from edge-tts response")
    return np.concatenate(out)


def _synth_mp3(text: str, voice: str) -> bytes:
    """Synthesize the full text into MP3 bytes via the Edge websocket.

    EDGE_TTS_PITCH is passed only when the installed edge-tts supports it
    (>= 6.1.10); older versions raise TypeError and we retry without it.
    """
    common = {"rate": _RATE, "volume": _VOLUME, "proxy": _PROXY}
    if _PITCH:
        try:
            com = edge_tts.Communicate(text, voice, pitch=_PITCH, **common)
        except TypeError:
            logger.warning("installed edge-tts does not support pitch; EDGE_TTS_PITCH ignored")
            com = edge_tts.Communicate(text, voice, **common)
    else:
        com = edge_tts.Communicate(text, voice, **common)
    buf = io.BytesIO()

    async def _run() -> None:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])

    # Synchronous entry: called from FastAPI sync routes (threadpool), where
    # asyncio.run is safe. Do NOT call from an async route directly.
    asyncio.run(_run())
    return buf.getvalue()


def synthesize_wav_bytes(
    text: str,
    voice: str,
    max_new_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = 50,
    seed: int = 42,
) -> tuple[bytes, int]:
    """Synthesize speech and return in-memory WAV bytes (no file written).

    Signature-compatible with tts_api.synthesize_wav_bytes; the sampling
    params are accepted and ignored (Edge service has no such knobs).
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    voice = resolve_voice(text, voice)
    mp3 = _synth_mp3(text, voice)
    pcm = _decode_to_pcm16(mp3)
    buf = io.BytesIO()
    sf.write(buf, pcm, SAMPLE_RATE, format="WAV")
    return buf.getvalue(), SAMPLE_RATE


def iter_pcm_chunks(
    text: str,
    voice: str,
    chunk_frames: int = 12,
    max_new_tokens: int = 256,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = 50,
    seed: int = 42,
):
    """Sentence-level streaming: split the text, synthesize each segment and
    yield its 16-bit PCM bytes (same contract as tts_api.iter_pcm_chunks).

    Each sentence round-trips the online service, so the first packet lands
    after the first sentence finishes; latency is comparable to Kokoro.
    """
    for seg in split_text(text):
        voice_resolved = resolve_voice(seg, voice)
        mp3 = _synth_mp3(seg, voice_resolved)
        pcm = _decode_to_pcm16(mp3)
        pcm_bytes = pcm.astype(np.int16).tobytes()
        yield 0, pcm_bytes
