"""Kokoro-82M TTS 本地推理（ONNX Runtime，纯 CPU）。

替代原 Audio8-TTS 实现（备份见 `tts_api.audio8.bak.py`）。

链路：
  文本 → espeak-ng 音素化（espeakng-runtime 直接加载 libespeak_ng.dll）→ kokorog2p
  phonemes_to_ids 映射到 Kokoro 词表 → ONNX 推理（onnx-community/Kokoro-82M-v1.0-ONNX）
  → 24kHz 音频。长文本按句子分段（split_text），逐段合成后 PCM 拼接，不受
  510 token 上下文限制截断。

模型/工具目录（可用环境变量覆盖）：
  KOKORO_MODEL_DIR  默认 ./models/Kokoro-82M-v1.0-ONNX
  ESPEAK_DIR        默认 ./third_party/espeak-ng（便携解包版，含 libespeak_ng.dll + espeak-ng-data，随仓库分发）
"""

import io
import logging
import os
import re
from pathlib import Path

import numpy as np
import onnxruntime as ort
import soundfile as sf

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # 项目根目录
MODEL_DIR = Path(os.getenv("KOKORO_MODEL_DIR", BASE_DIR / "models" / "Kokoro-82M-v1.0-ONNX"))
ESPEAK_DIR = Path(os.getenv("ESPEAK_DIR", BASE_DIR / "third_party" / "espeak-ng"))
SAMPLE_RATE = 24000

_espeak_rt = None
_session = None
_voice_list: list[str] | None = None
_voice_style_cache: dict[str, np.ndarray] = {}
_phonemes_to_ids = None


def get_runtime() -> tuple[object, ort.InferenceSession]:
    """返回 (espeakng_runtime.EspeakRuntime, onnxruntime.InferenceSession) 单例。"""
    global _espeak_rt, _session
    if _espeak_rt is None:
        from espeakng_runtime import EspeakRuntime
        _espeak_rt = EspeakRuntime(
            library=str(ESPEAK_DIR / "libespeak_ng.dll"),
            data=str(ESPEAK_DIR / "espeak-ng-data"),
        )
    if _session is None:
        _session = ort.InferenceSession(
            str(MODEL_DIR / "onnx" / "model_fp16.onnx"),
            providers=["CPUExecutionProvider"],
        )
    return _espeak_rt, _session


def _load_phonemes_to_ids():
    global _phonemes_to_ids
    if _phonemes_to_ids is None:
        from kokorog2p import phonemes_to_ids
        _phonemes_to_ids = phonemes_to_ids
    return _phonemes_to_ids


def _phonemize_ids(text: str) -> list[int]:
    """文本 → espeak 音素 → Kokoro token ids。"""
    rt, _ = get_runtime()
    phonemes = rt.phonemize(text, voice="en-us")
    ids = _load_phonemes_to_ids()(phonemes)
    if not ids:
        raise ValueError(f"phonemization produced no tokens: {text!r}")
    return ids


def _voice_style(voice: str, n: int) -> np.ndarray:
    """返回 (1, 256) 风格向量：voices/<voice>.bin 的第 n 行（Kokoro 约定）。"""
    style = _voice_style_cache.get(voice)
    if style is None:
        path = MODEL_DIR / "voices" / f"{voice}.bin"
        if not path.exists():
            raise ValueError(f"voice not found: {voice}")
        style = np.fromfile(path, dtype=np.float32).reshape(-1, 1, 256)
        _voice_style_cache[voice] = style
    return style[n]


def list_voices() -> list[dict]:
    """返回已下载的 Kokoro 音色列表（voices/*.bin）。"""
    global _voice_list
    if _voice_list is None:
        voices_dir = MODEL_DIR / "voices"
        if voices_dir.is_dir():
            _voice_list = sorted(p.stem for p in voices_dir.glob("*.bin") if not p.stem.startswith("."))
        else:
            _voice_list = []
    return [{"name": name} for name in _voice_list]


def voice_exists(voice: str) -> bool:
    return (MODEL_DIR / "voices" / f"{voice}.bin").exists()


def split_text(text: str, max_chars: int = 120) -> list[str]:
    """按句子边界将长文本切分为多段，避免单段超出 Kokoro 510 token 上下文。

    Args:
        text: 待切分文本。
        max_chars: 单段最大字符数（中文/日文按字符计，英文按字符计）。

    Returns:
        list[str]: 非空分段列表。
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
    # 合并过短的分段（避免碎片化）
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) + len(part) <= max_chars:
            merged[-1] += part
        else:
            merged.append(part)
    return merged or [text]


def _synthesize_segment(text: str, voice: str) -> np.ndarray:
    """合成单段文本，返回 24kHz float32 音频（一维）。"""
    _, sess = get_runtime()
    ids = _phonemize_ids(text)
    if len(ids) > 510:
        # 理论不应发生（split_text 已限长）；保险起见按 510 截断
        ids = ids[:510]
    style = _voice_style(voice, len(ids))
    tokens = np.array([[0, *ids, 0]], dtype=np.int64)
    audio = sess.run(
        None,
        dict(input_ids=tokens, style=style, speed=np.ones(1, dtype=np.float32)),
    )[0]
    return audio[0].astype(np.float32)


def synthesize_wav_bytes(
    text: str,
    voice: str,
    max_new_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = 50,
    seed: int = 42,
) -> tuple[bytes, int]:
    """合成语音并返回内存中的 WAV 字节（不落盘）。

    与 Audio8 版签名兼容（max_new_tokens 等参数保留仅为兼容，Kokoro 不使用）。
    长文本按句子自动分段合成，PCM 无缝拼接后一次性编码 WAV。

    Returns:
        tuple[bytes, int]: (WAV 文件字节, 采样率 24000)。
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
    """流式合成：长文本按句子分段，逐段合成后 yield 16-bit PCM 字节块。

    与 Audio8 版签名兼容（chunk_frames/max_new_tokens 等参数保留仅为兼容，
    Kokoro 为句子级流式，每段整段合成后产出）。
    """
    for seg in split_text(text):
        audio = _synthesize_segment(seg, voice)
        pcm = (audio * 32767.0).astype(np.int16).tobytes()
        yield 0, pcm
