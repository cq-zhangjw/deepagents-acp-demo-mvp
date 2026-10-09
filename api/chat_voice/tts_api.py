"""Kokoro TTS 本地推理（ONNX Runtime，纯 CPU）。

替代原 Audio8-TTS 实现（备份见 `tts_api.audio8.bak.py`）。自 v1.1-zh 起支持双模型：
  - zh 文本 → Kokoro-82M-v1.1-zh（models/Kokoro-82M-v1.1-zh-ONNX，中文音色 zf_*/zm_*，词表 model='1.1-zh'）
  - en / ja 文本 → Kokoro-82M-v1.0（models/Kokoro-82M-v1.0-ONNX，英文音色 af_*/am_*/bf_*/bm_*，词表 model='1.0'）

链路（与 v1.0 相同）：
  文本 → espeak-ng 音素化（espeakng-runtime 直接加载 libespeak_ng.dll）→ kokorog2p
  phonemes_to_ids 映射（按语言选词表）→ ONNX 推理 → 24kHz 音频。
  长文本按句子分段（split_text），逐段合成后 PCM 拼接，不受 token 上下文限制截断。

模型/工具目录（可用环境变量覆盖）：
  KOKORO_MODEL_DIR_EN   默认 ./models/Kokoro-82M-v1.0-ONNX（英文）
  KOKORO_MODEL_DIR_ZH   默认 ./models/Kokoro-82M-v1.1-zh-ONNX（中文）
  ESPEAK_DIR            默认 ./third_party/espeak-ng（便携解包版，随仓库分发）
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
MODEL_DIR_EN = Path(os.getenv("KOKORO_MODEL_DIR_EN", BASE_DIR / "models" / "Kokoro-82M-v1.0-ONNX"))
MODEL_DIR_ZH = Path(os.getenv("KOKORO_MODEL_DIR_ZH", BASE_DIR / "models" / "Kokoro-82M-v1.1-zh-ONNX"))
ESPEAK_DIR = Path(os.getenv("ESPEAK_DIR", BASE_DIR / "third_party" / "espeak-ng"))
SAMPLE_RATE = 24000

# 语言 → 模型目录 / 词表 variant / 模型文件名
# 注：v1.1-zh 的 model_fp16.onnx 在 onnxruntime 1.24 CPU 上加载即崩（IR v9 兼容问题），
#     中文统一使用 fp32 model.onnx；v1.0 的 fp16 正常。
_MODEL_FOR_LANG = {"zh": MODEL_DIR_ZH, "ja": MODEL_DIR_EN, "en": MODEL_DIR_EN}
_VOCAB_FOR_LANG = {"zh": "1.1-zh", "ja": "1.0", "en": "1.0"}
_MODEL_FILE_FOR_LANG = {"zh": "model.onnx", "ja": "model_fp16.onnx", "en": "model_fp16.onnx"}

_espeak_rt = None
_session_cache: dict[str, ort.InferenceSession] = {}
_voice_style_cache: dict[str, np.ndarray] = {}
_phonemes_to_ids = None

# 各语言默认音色（env 可覆盖；zh 为 v1.1-zh 中文音色，en 为 v1.0 英文音色，ja 暂用英文音色读 CJK 音素）
_DEFAULT_VOICES = {
    "zh": os.getenv("TTS_VOICE_ZH", "zf_xiaoxiao"),
    "ja": os.getenv("TTS_VOICE_JA", "af_bella"),
    "en": os.getenv("TTS_VOICE_EN", "af_heart"),
}
# espeak-ng 音素化 voice（按语言；ja 缺 mbrola 库，暂用 cmn 读汉字，假名会回退）
_ESPEAK_VOICE_FOR_LANG = {"zh": "cmn", "ja": "cmn", "en": "en-us"}
# v1.1-zh 中文音色前缀（判断 voice 属于哪个模型目录）
_ZH_VOICE_PREFIXES = ("zf_", "zm_")


def detect_language(text: str) -> str:
    """轻量语言判别：含日文假名 → ja；含 CJK 汉字 → zh；否则 en。"""
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
    """音色所属模型目录：zf_*/zm_* → v1.1-zh，其余 → v1.0。"""
    if voice.startswith(_ZH_VOICE_PREFIXES):
        return MODEL_DIR_ZH
    return MODEL_DIR_EN


def get_espeak() -> object:
    """返回 espeakng_runtime.EspeakRuntime 单例。"""
    global _espeak_rt
    if _espeak_rt is None:
        from espeakng_runtime import EspeakRuntime
        _espeak_rt = EspeakRuntime(
            library=str(ESPEAK_DIR / "libespeak_ng.dll"),
            data=str(ESPEAK_DIR / "espeak-ng-data"),
        )
    return _espeak_rt


def get_session(lang: str) -> ort.InferenceSession:
    """按语言返回 ONNX session（每个模型目录一个 session，按需惰性加载）。"""
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


def _phonemize_ids(text: str, lang: str) -> list[int]:
    """文本 → espeak 音素 → Kokoro token ids（按语言选 espeak voice 与词表）。"""
    espeak = get_espeak()
    phonemes = espeak.phonemize(text, voice=_ESPEAK_VOICE_FOR_LANG.get(lang, "en-us"))
    ids = _load_phonemes_to_ids()(phonemes, model=_VOCAB_FOR_LANG.get(lang, "1.0"))
    if not ids:
        raise ValueError(f"phonemization produced no tokens: {text!r}")
    return ids


def _voice_style(voice: str, n: int) -> np.ndarray:
    """返回 (1, 256) 风格向量：对应模型目录 voices/<voice>.bin 的第 n 行（Kokoro 约定）。"""
    style = _voice_style_cache.get(voice)
    if style is None:
        path = _voice_model_dir(voice) / "voices" / f"{voice}.bin"
        if not path.exists():
            raise ValueError(f"voice not found: {voice}")
        style = np.fromfile(path, dtype=np.float32).reshape(-1, 1, 256)
        _voice_style_cache[voice] = style
    return style[n]


def list_voices() -> list[dict]:
    """返回全部已下载音色（v1.0 英文 + v1.1-zh 中文）。

    每次实时扫描两个模型的 voices 文件夹（*.bin 文件名即音色名），
    本地增删音色文件后无需重启即可生效。
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
    """语言默认音色；若 env 配置的音色名不存在（如误配），回退到该语言第一个可用音色。"""
    default = _DEFAULT_VOICES.get(lang, "af_bella")
    if voice_exists(default):
        return default
    for item in list_voices():
        name = item["name"]
        if lang == "zh" and name.startswith(_ZH_VOICE_PREFIXES):
            return name
        if lang != "zh" and not name.startswith(_ZH_VOICE_PREFIXES):
            return name
    return "af_bella"


def resolve_voice(text: str, voice: str) -> str:
    """将请求音色解析为实际音色名：
    1) voice 为已存在的音色名 → 直接使用（尊重显式选择）；
    2) voice 缺失 / 旧 Audio8 名（zh/ja/en 等）/ 未知 → 按文本语言取对应默认音色。
    """
    voice = (voice or "").strip()
    if voice and voice_exists(voice):
        return voice
    return _fallback_voice(detect_language(text))


def split_text(text: str, max_chars: int = 120) -> list[str]:
    """按句子边界将长文本切分为多段，避免单段超出 Kokoro token 上下文。

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
    lang = detect_language(text)
    sess = get_session(lang)
    ids = _phonemize_ids(text, lang)
    if len(ids) > 510:
        # 理论不应发生（split_text 已限长）；保险起见按 510 截断
        ids = ids[:510]
    style = _voice_style(voice, len(ids))
    tokens = np.array([[0, *ids, 0]], dtype=np.int64)
    audio = sess.run(
        None,
        dict(input_ids=tokens, style=style, speed=np.ones(1, dtype=np.float32)),
    )[0]
    # 兼容输出维度：v1.0 为 (1, N)，v1.1-zh 为 (N,)
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
