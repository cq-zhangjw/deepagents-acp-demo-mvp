"""Windows SAPI fallback TTS (system speech voices, no model files).

Used when TTS_ENGINE=sapi directly, or as an automatic fallback when
TTS_FALLBACK=true and the primary engine (genie / edge_tts) fails.
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

Public API mirrors the genie engine interface / edge_tts.py:
  synthesize_wav_bytes / iter_pcm_chunks / list_voices / resolve_voice
"""

import io
import logging
import os
import wave
from typing import Iterator

from dotenv import load_dotenv

from .tts_common import detect_language, split_language_segments, split_text

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


def _sapi_rate() -> int:
    """Map SAPI_SPEAK_RATE multiplier to SAPI SpVoice.Rate (-10..10).

    SAPI Rate is a coarse integer scale; a linear map (mult-1)*10 gives a good
    feel (1.3x -> 3). Clamped to the valid range.
    """
    mult = os.getenv("SAPI_SPEAK_RATE", "").strip()
    if not mult:
        return 0
    try:
        return max(-10, min(10, round((float(mult) - 1.0) * 10)))
    except ValueError:
        logger.warning("invalid SAPI_SPEAK_RATE=%r; using normal rate", mult)
        return 0


_SPEAK_RATE = _sapi_rate()

_voice_cache: list[dict] | None = None


def _sp_voice():
    """Return a fresh SAPI.SpVoice COM object (per-call; COM is STA-bound)."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    sp = win32com.client.Dispatch("SAPI.SpVoice")
    sp.Rate = _SPEAK_RATE
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = _SAFT24KHZ16BITMONO
    stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
    stream.Format = fmt
    sp.AudioOutputStream = stream
    return sp, stream


def _token_descriptions() -> list[str]:
    import pythoncom
    import win32com.client

    # Ensure COM is initialized on the current thread; the streaming generator
    # runs in a fresh FastAPI worker thread where COM is not yet initialized
    # ("CoInitialize has not been called" otherwise). CoInitialize is reference
    # counted, so this balances its CoUninitialize safely.
    pythoncom.CoInitialize()
    try:
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        voices = sp.GetVoices()
        descs = [voices.Item(i).GetDescription() for i in range(voices.Count)]
        # release COM references before CoUninitialize to avoid IUnknown-release warnings
        del voices, sp
        return descs
    finally:
        pythoncom.CoUninitialize()


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
    """Resolve a system voice by description.

    'auto' (or empty) is kept as the sentinel 'auto' so the caller / iter_pcm_chunks
    splits the text per language and reads each span with its own system voice.
    Otherwise: explicit voice (description substring) > SAPI_VOICE_<LANG> fragment
    > first installed voice matching the language keyword. An unknown/legacy voice
    name (e.g. 'zh') never leaks into SAPI; it falls through to the per-language pick.
    """
    v = (voice or "").strip()
    if not v or v.lower() == "auto":
        return "auto"
    return _match_voice(detect_language(text), v)


def _wav_from_pcm(pcm: bytes) -> bytes:
    """Wrap raw PCM into a WAV container (24 kHz 16-bit mono)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_CHANNELS)
        w.setsampwidth(_SAMPWIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _resample_pcm16(pcm: bytes, src_rate: int) -> bytes:
    """Linear-interpolate 16-bit PCM to SAMPLE_RATE (defensive resample)."""
    if src_rate == SAMPLE_RATE:
        return pcm
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return pcm
    n = max(1, int(round(len(x) * SAMPLE_RATE / src_rate)))
    y = np.interp(np.linspace(0.0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


def _normalize_pcm(data: bytes, rate: int, bits: int, channels: int) -> bytes:
    """Normalize raw SAPI PCM to 24 kHz 16-bit mono.

    SAPI frequently ignores the requested SAFT24kHz16BitMono format and emits
    8-bit unsigned and/or stereo PCM; without converting bit depth and channels
    the frontend (which assumes 16-bit mono) plays pure noise.
    """
    import numpy as np

    if not data:
        return data
    if bits == 8:
        # 8-bit PCM is unsigned (0..255, 128 = silence)
        samples = (np.frombuffer(data, dtype=np.uint8).astype(np.int16) - 128) * 256
    elif bits == 16:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.int16)
    else:
        logger.warning("sapi output bits=%s unsupported; passing through", bits)
        samples = np.frombuffer(data, dtype=np.int16)
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1).astype(np.int16)
    pcm = samples.astype(np.int16).tobytes()
    if rate != SAMPLE_RATE:
        pcm = _resample_pcm16(pcm, rate)
    return pcm


def _synthesize_segment(text: str, voice: str) -> bytes:
    """Synthesize one segment; returns WAV bytes at SAMPLE_RATE.

    The SAPI output format is read back (WaveFormatEx) and resampled to
    24 kHz 16-bit mono when the system SAPI downgraded the requested format,
    so playback never runs at the wrong speed (no harsh/glitchy sound).
    """
    import pythoncom

    try:
        sp, stream = _sp_voice()
        descs = _token_descriptions()
        for i, d in enumerate(descs):
            if voice.lower() in d.lower() or d.lower() in voice.lower():
                sp.Voice = sp.GetVoices().Item(i)
                break
        sp.Speak(text)  # synchronous
        data = bytes(stream.GetData())
        # read back the actual output format (SAPI often downgrades the requested
        # SAFT24kHz16BitMono to e.g. 8-bit stereo; must normalize or it is noise)
        is_riff = data[:4] == b"RIFF"
        try:
            fmt_info = stream.Format.GetWaveFormatEx()
            rate = int(fmt_info.SamplesPerSec)
            bits = int(fmt_info.BitsPerSample)
            channels = int(fmt_info.Channels)
            del fmt_info
        except Exception:  # noqa: BLE001
            rate, bits, channels = SAMPLE_RATE, 16, _CHANNELS
        # release COM references before CoUninitialize to avoid IUnknown-release warnings
        del sp, stream
        # GetData returns raw PCM without a RIFF header; wrap it as WAV
        if is_riff:
            return data
        data = _normalize_pcm(data, rate, bits, channels)
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

    Signature-compatible with genie engine synthesize_wav_bytes; sampling params
    are accepted and ignored (SAPI has no such knobs).
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    resolved = resolve_voice(text, voice)
    if resolved == "auto":
        # per-language segments concatenated into one WAV (mixed-voice reading)
        import numpy as np

        parts: list[np.ndarray] = []
        for lang, seg_text in split_language_segments(text) or [("zh", text)]:
            wav = _synthesize_segment(seg_text, _match_voice(lang, ""))
            with io.BytesIO(wav) as b:
                w = wave.open(b, "rb")
                parts.append(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16))
        pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        return _wav_from_pcm(pcm.tobytes()), SAMPLE_RATE
    return _synthesize_segment(text, resolved), SAMPLE_RATE


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
    """Segment-level streaming, same contract as the genie engine iter_pcm_chunks.

    Empty/'auto' voice -> split the text per language (zh/ja/en) and read each
    span with that language's system voice, so mixed text is not read entirely
    in one language. A concrete voice -> sentence-split read by that voice.
    """
    v = (voice or "").strip().lower()
    if v in ("", "auto"):
        segs = split_language_segments(text)
        logger.info("[sapi auto] split into %d segments:", len(segs))
        for lang, seg_text in segs:
            seg_voice = _match_voice(lang, "")
            logger.info("  [%s] -> %s | %r", lang, seg_voice, seg_text)
            wav = _synthesize_segment(seg_text, seg_voice)
            with io.BytesIO(wav) as b:
                w = wave.open(b, "rb")
                pcm = w.readframes(w.getnframes())
            yield 0, pcm
        return
    logger.info("[sapi voice=%s] not auto; sentence-split only", voice)
    for seg in split_text(text):
        wav = _synthesize_segment(seg, resolve_voice(seg, voice))
        with io.BytesIO(wav) as b:
            w = wave.open(b, "rb")
            pcm = w.readframes(w.getnframes())
        yield 0, pcm
