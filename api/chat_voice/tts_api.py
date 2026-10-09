"""Kokoro TTS local inference (ONNX Runtime, CPU only).

Replaces the legacy Audio8-TTS implementation. Multi-model support since v1.1-zh:
  - zh text -> Kokoro-82M-v1.1-zh (models/Kokoro-82M-v1.1-zh-ONNX, zh voices zf_*/zm_*, vocab model='1.1-zh')
  - ja text -> Kokoro-82M-v1.0 (models/Kokoro-82M-v1.0-ONNX, ja voices jf_*/jm_*, vocab model='1.0';
    v1.0 is a multilingual model with built-in Japanese voices/phonemes, no separate ja model needed)
  - en text -> Kokoro-82M-v1.0 (models/Kokoro-82M-v1.0-ONNX, en voices af_*/am_*/bf_*/bm_*, vocab model='1.0')

Pipeline (same as v1.0):
  text -> espeak-ng phonemization (espeakng-runtime loads libespeak_ng.dll directly) -> kokorog2p
  phonemes_to_ids mapping (vocab selected by language) -> ONNX inference -> 24kHz audio.
  Long text is split into sentence segments (split_text) and synthesized segment by segment;
  PCM is concatenated, so there is no token-context truncation.

Model/tool directories (overridable via env):
  KOKORO_MODEL_DIR_EN   default ./models/Kokoro-82M-v1.0-ONNX (shared by en/ja)
  KOKORO_MODEL_DIR_ZH   default ./models/Kokoro-82M-v1.1-zh-ONNX (zh)
  ESPEAK_DIR            default ./third_party/espeak-ng (portable unpack, shipped with repo)
"""

import io
import logging
import os
import re
from pathlib import Path

import numpy as np
import onnxruntime as ort
import soundfile as sf
from dotenv import load_dotenv

load_dotenv()  # ensure .env TTS_* settings take effect (module-level getenv depends on this)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # project root
MODEL_DIR_EN = Path(os.getenv("KOKORO_MODEL_DIR_EN", BASE_DIR / "models" / "Kokoro-82M-v1.0-ONNX"))
MODEL_DIR_ZH = Path(os.getenv("KOKORO_MODEL_DIR_ZH", BASE_DIR / "models" / "Kokoro-82M-v1.1-zh-ONNX"))
ESPEAK_DIR = Path(os.getenv("ESPEAK_DIR", BASE_DIR / "third_party" / "espeak-ng"))
SAMPLE_RATE = 24000

# language -> model dir / vocab variant / model file name
# Note: v1.1-zh model_fp16.onnx crashes on onnxruntime 1.24 CPU (IR v9 compatibility issue),
#       so zh always uses fp32 model.onnx; v1.0 (incl. ja) fp16 works fine.
_MODEL_FOR_LANG = {"zh": MODEL_DIR_ZH, "ja": MODEL_DIR_EN, "en": MODEL_DIR_EN}
_VOCAB_FOR_LANG = {"zh": "1.1-zh", "ja": "1.0", "en": "1.0"}
_MODEL_FILE_FOR_LANG = {"zh": "model.onnx", "ja": "model_fp16.onnx", "en": "model_fp16.onnx"}

_espeak_rt = None
_session_cache: dict[str, ort.InferenceSession] = {}
_voice_style_cache: dict[str, np.ndarray] = {}
_phonemes_to_ids = None

# default voice per language (env overridable: TTS_VOICE_ZH / TTS_VOICE_JA / TTS_VOICE_EN)
_DEFAULT_VOICES = {
    "zh": os.getenv("TTS_VOICE_ZH", "zf_001"),
    "ja": os.getenv("TTS_VOICE_JA", "jf_alpha"),
    "en": os.getenv("TTS_VOICE_EN", "af_heart"),
}
# espeak-ng phonemization voice per language
_ESPEAK_VOICE_FOR_LANG = {"zh": "cmn", "ja": "ja", "en": "en-us"}
# voice name prefixes: v1.1-zh zh voices / v1.0 ja voices (used to pick the model dir)
_ZH_VOICE_PREFIXES = ("zf_", "zm_")
_JA_VOICE_PREFIXES = ("jf_", "jm_")


def detect_language(text: str) -> str:
    """Lightweight language detection: kana -> ja; CJK hanzi -> zh; otherwise en."""
    if not text:
        return "en"
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff\uff66-\uff9f]", text):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en"


def _model_dir_for_lang(lang: str) -> Path:
    return _MODEL_FOR_LANG.get(lang, MODEL_DIR_EN)


def _voice_model_dir(voice: str) -> Path:
    """Model dir the voice belongs to: zf_*/zm_* -> v1.1-zh; jf_*/jm_* and others -> v1.0 (multilingual)."""
    if voice.startswith(_ZH_VOICE_PREFIXES):
        return MODEL_DIR_ZH
    return MODEL_DIR_EN


def get_espeak() -> object:
    """Return the espeakng_runtime.EspeakRuntime singleton."""
    global _espeak_rt
    if _espeak_rt is None:
        from espeakng_runtime import EspeakRuntime
        _espeak_rt = EspeakRuntime(
            library=str(ESPEAK_DIR / "libespeak_ng.dll"),
            data=str(ESPEAK_DIR / "espeak-ng-data"),
        )
    return _espeak_rt


def get_session(lang: str) -> ort.InferenceSession:
    """Return the ONNX session for a language (one session per model dir, lazy-loaded)."""
    session = _session_cache.get(lang)
    if session is None:
        model_path = _model_dir_for_lang(lang) / "onnx" / _MODEL_FILE_FOR_LANG.get(lang, "model_fp16.onnx")
        if not model_path.exists():
            raise FileNotFoundError(f"model not found: {model_path}")
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        _session_cache[lang] = session
    return session


def _load_phonemes_to_ids():
    global _phonemes_to_ids
    if _phonemes_to_ids is None:
        from kokorog2p import phonemes_to_ids
        _phonemes_to_ids = phonemes_to_ids
    return _phonemes_to_ids


_kakasi = None


def _to_romaji(text: str) -> str:
    """Convert Japanese text (kanji + kana) to romaji.

    Uses pykakasi (pure Python, built-in dictionary): both kanji and kana are
    converted to Hepburn romaji, which is then phonemized with espeak en-us
    (avoids espeak-ng's ja voice dependency on mbrola).
    """
    global _kakasi
    if _kakasi is None:
        from pykakasi import kakasi
        _kakasi = kakasi()
    return "".join(item["hepburn"] for item in _kakasi.convert(text))


def _phonemize_ids(text: str, lang: str) -> list[int]:
    """Text -> espeak phonemes -> Kokoro token ids (voice/vocab selected by language).

    Japanese special path: espeak-ng's ja voice requires the mbrola library
    (not bundled here), so Japanese is first converted to romaji via pykakasi,
    phonemized with en-us voice, and mapped against the v1.0 vocab
    (Kokoro v1.0 is multilingual and includes Japanese phonemes; the result is
    a stable approximation of the Japanese accent).
    """
    espeak = get_espeak()
    if lang == "ja":
        text = _to_romaji(text)
        voice = "en-us"
    else:
        voice = _ESPEAK_VOICE_FOR_LANG.get(lang, "en-us")
    phonemes = espeak.phonemize(text, voice=voice)
    ids = _load_phonemes_to_ids()(phonemes, model=_VOCAB_FOR_LANG.get(lang, "1.0"))
    if not ids:
        raise ValueError(f"phonemization produced no tokens: {text!r}")
    return ids


def _voice_style(voice: str, n: int) -> np.ndarray:
    """Return a (1, 256) style vector: row n of voices/<voice>.bin (Kokoro convention).

    n is the phoneme token count; the .bin row count is limited (e.g. 510 rows
    for v1.1-zh, index 0..509). When n exceeds the row count, the last row is
    used to avoid an index-out-of-bounds error.
    """
    style = _voice_style_cache.get(voice)
    if style is None:
        path = _voice_model_dir(voice) / "voices" / f"{voice}.bin"
        if not path.exists():
            raise ValueError(f"voice not found: {voice}")
        style = np.fromfile(path, dtype=np.float32).reshape(-1, 1, 256)
        _voice_style_cache[voice] = style
    return style[min(n, len(style) - 1)]


def list_voices() -> list[dict]:
    """Return all downloaded voices (v1.0 multilingual en/ja + v1.1-zh zh).

    Scans the voices folders of both models in real time (*.bin stem == voice
    name), so adding/removing voice files locally takes effect without restart.
    """
    names: set[str] = set()
    for model_dir in (MODEL_DIR_EN, MODEL_DIR_ZH):
        voices_dir = model_dir / "voices"
        if voices_dir.is_dir():
            names.update(p.stem for p in voices_dir.glob("*.bin") if not p.stem.startswith("."))
    return [{"name": name} for name in sorted(names)]


def voice_exists(voice: str) -> bool:
    return (_voice_model_dir(voice) / "voices" / f"{voice}.bin").exists()


def _fallback_voice(lang: str) -> str:
    """Language default voice; falls back to the first available voice if the
    env-configured name is missing (e.g. misconfigured)."""
    default = _DEFAULT_VOICES.get(lang, "af_bella")
    if voice_exists(default):
        return default
    for item in list_voices():
        name = item["name"]
        if lang == "zh" and name.startswith(_ZH_VOICE_PREFIXES):
            return name
        if lang == "ja" and name.startswith(_JA_VOICE_PREFIXES):
            return name
        if lang == "en" and not name.startswith(_ZH_VOICE_PREFIXES) and not name.startswith(_JA_VOICE_PREFIXES):
            return name
    return "af_bella"


def resolve_voice(text: str, voice: str) -> str:
    """Resolve the requested voice to an actual voice name:
    1) if voice exists -> use it as-is (respect explicit choice);
    2) voice missing / legacy Audio8 name (zh/ja/en etc.) / unknown -> use the
       language default voice for the detected text language.
    """
    voice = (voice or "").strip()
    if voice and voice_exists(voice):
        return voice
    return _fallback_voice(detect_language(text))


def split_text(text: str, max_chars: int = 120) -> list[str]:
    """Split long text into segments on sentence boundaries to stay within the
    Kokoro token context.

    Args:
        text: text to split.
        max_chars: max characters per segment (counted per character for zh/ja/en).

    Returns:
        list[str]: non-empty segment list.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？；…\n.!?;":
            if len(buf) >= max_chars * 0.4 and (buf.count("。") + buf.count("！") + buf.count("？") >= 1):
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""
        elif len(buf) >= max_chars:
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    # merge overly short segments (avoid fragmentation)
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) + len(part) <= max_chars:
            merged[-1] += part
        else:
            merged.append(part)
    return merged or [text]


def _synthesize_segment(text: str, voice: str) -> np.ndarray:
    """Synthesize a single segment and return 24kHz float32 audio (1-D)."""
    lang = detect_language(text)
    sess = get_session(lang)
    ids = _phonemize_ids(text, lang)
    if len(ids) > 510:
        # should not happen (split_text limits length); truncate to 510 defensively
        ids = ids[:510]
    style = _voice_style(voice, len(ids))
    tokens = np.array([[0, *ids, 0]], dtype=np.int64)
    audio = sess.run(
        None,
        dict(input_ids=tokens, style=style, speed=np.ones(1, dtype=np.float32)),
    )[0]
    # normalize output shape: v1.0 is (1, N), v1.1-zh is (N,)
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    return audio


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

    Signature-compatible with the Audio8 version (max_new_tokens etc. kept only
    for compatibility; Kokoro ignores them). Long text is split into sentence
    segments, synthesized, and the PCM is seamlessly concatenated into one WAV.

    Returns:
        tuple[bytes, int]: (WAV file bytes, sample rate 24000).
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    segments = split_text(text)
    pcm_parts: list[np.ndarray] = []
    for seg in segments:
        pcm_parts.append(_synthesize_segment(seg, voice))
    full = np.concatenate(pcm_parts) if len(pcm_parts) > 1 else pcm_parts[0]
    buf = io.BytesIO()
    sf.write(buf, full, SAMPLE_RATE, format="WAV")
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
    """Streaming synthesis: long text is split into sentence segments and each
    segment yields a 16-bit PCM byte block.

    Signature-compatible with the Audio8 version (chunk_frames/max_new_tokens
    kept only for compatibility; Kokoro is sentence-level streaming, each
    segment is fully synthesized before being yielded).
    """
    for seg in split_text(text):
        audio = _synthesize_segment(seg, voice)
        pcm = (audio * 32767.0).astype(np.int16).tobytes()
        yield 0, pcm
