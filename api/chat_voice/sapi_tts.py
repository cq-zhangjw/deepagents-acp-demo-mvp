"""Windows SAPI fallback TTS (system speech voices, no model files).

Used when TTS_ENGINE=sapi directly, or as an automatic fallback when
TTS_FALLBACK=true and the primary engine (kokoro / edge_tts) fails.
Synthesizes through the system SAPI voices via win32com, in memory, as
24 kHz 16-bit mono (SAPI SAFT24kHz16BitMono), no files are written.

Env controls:
  SAPI_VOICE_ZH / SAPI_VOICE_JA / SAPI_VOICE_EN
      optional preferred voice name fragment per language; empty = auto-pick
      the first installed system voice whose description matches the detected
      text language (Chinese / Japanese / English).
  TTS_FALLBACK (true|false)
      master switch for the fallback path (handled in chat_voice.py).

Windows only (win32com). On non-Windows platforms importing this module
raises ImportError and the fallback is skipped.

Public API mirrors tts_api.py / edge_tts.py:
  synthesize_wav_bytes / iter_pcm_chunks / list_voices / resolve_voice
"""

import io
import logging
import os
import wave
from typing import Iterator

from dotenv import load_dotenv

from .tts_api import detect_language, split_text

load_dotenv()

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
_CHANNELS = 1
_SAMPWIDTH = 2
# SAPI SpeechAudioFormatType: SAFT24kHz16BitMono
_SAFT24KHZ16BITMONO = 25

_PREFERRED = {
    "zh": os.getenv("SAPI_VOICE_ZH", "").strip(),
    "ja": os.getenv("SAPI_VOICE_JA", "").strip(),
    "en": os.getenv("SAPI_VOICE_EN", "").strip(),
}
# description keywords used to pick a voice when no explicit name is given
_LANG_KEYWORD = {"zh": "Chinese", "ja": "Japanese", "en": "English"}

_voice_cache: list[dict] | None = None


def _sp_voice():
    """Return a fresh SAPI.SpVoice COM object (per-call; COM is STA-bound)."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    sp = win32com.client.Dispatch("SAPI.SpVoice")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = _SAFT24KHZ16BITMONO
    stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
    stream.Format = fmt
    sp.AudioOutputStream = stream
    return sp, stream


def _token_descriptions() -> list[str]:
    import win32com.client

    sp = win32com.client.Dispatch("SAPI.SpVoice")
    voices = sp.GetVoices()
    return [voices.Item(i).GetDescription() for i in range(voices.Count)]


def list_voices() -> list[dict]:
    """Return installed system SAPI voices: [{name, label}] (description text)."""
    global _voice_cache
    if _voice_cache is not None:
        return _voice_cache
    try:
        descs = _token_descriptions()
    except Exception as exc:  # noqa: BLE001
        logger.error("sapi list voices failed: %s", exc)
        return []
    _voice_cache = [{"name": d, "label": d} for d in descs]
    return _voice_cache


def _match_voice(lang: str, voice: str) -> str:
    """Pick a system voice by description.

    Priority: explicit voice (description substring) > SAPI_VOICE_<LANG>
    fragment > first installed voice matching the language keyword.
    """
    descs = _token_descriptions()
    if voice:
        for d in descs:
            if voice.lower() in d.lower():
                return d
    preferred = _PREFERRED.get(lang, "")
    if preferred:
        for d in descs:
            if preferred.lower() in d.lower():
                return d
    keyword = _LANG_KEYWORD.get(lang, "English")
    for d in descs:
        if keyword.lower() in d.lower():
            return d
    if descs:
        return descs[0]
    raise RuntimeError("no SAPI voice available on this system")


def resolve_voice(text: str, voice: str) -> str:
    voice = (voice or "").strip()
    if not voice:
        voice = _match_voice(detect_language(text), "")
    return voice


def _wav_from_pcm(pcm: bytes) -> bytes:
    """Wrap raw PCM into a WAV container (24 kHz 16-bit mono)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_CHANNELS)
        w.setsampwidth(_SAMPWIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _synthesize_segment(text: str, voice: str) -> bytes:
    """Synthesize one segment; returns (pcm_bytes, rate=24000)."""
    import pythoncom

    try:
        sp, stream = _sp_voice()
        for i, d in enumerate(_token_descriptions()):
            if voice == d:
                sp.Voice = sp.GetVoices().Item(i)
                break
        sp.Speak(text)  # synchronous
        data = bytes(stream.GetData())
        # GetData returns raw PCM without a RIFF header; wrap it as WAV
        if data[:4] == b"RIFF":
            return data
        return _wav_from_pcm(data)
    finally:
        pythoncom.CoUninitialize()


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

    Signature-compatible with tts_api.synthesize_wav_bytes; sampling params
    are accepted and ignored (SAPI has no such knobs).
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    voice = resolve_voice(text, voice)
    return _synthesize_segment(text, voice), SAMPLE_RATE


def iter_pcm_chunks(
    text: str,
    voice: str,
    chunk_frames: int = 12,
    max_new_tokens: int = 256,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = 50,
    seed: int = 42,
) -> Iterator[tuple[int, bytes]]:
    """Sentence-level streaming, same contract as tts_api.iter_pcm_chunks."""
    for seg in split_text(text):
        wav = _synthesize_segment(seg, resolve_voice(seg, voice))
        # strip the WAV header (44 bytes for standard PCM) and yield PCM
        pcm = wav[44:]
        yield 0, pcm
