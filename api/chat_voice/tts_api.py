#!/usr/bin/env python3
"""Audio8 TTS ONNX Runtime 封装 API。

提供 synthesize() 函数：输入文本和音色名，返回保存的 WAV 音频路径。
运行时基于 onnx_runtime/ 下的 0.6B INT4 纯 CPU 推理，不依赖 PyTorch。

用法（在 .venv 环境下）:
    from tts_api import synthesize, list_voices, register_voice

    path = synthesize("你好，世界", voice="zh")
    print(path)

命令行:
    python tts_api.py --text "你好" --voice zh [--output out.wav]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import soundfile as sf
import numpy as np

# 将 onnx_runtime 包目录加入导入路径
_ONNX_RUNTIME_DIR = Path(__file__).resolve().parent / "onnx_runtime"
sys.path.insert(0, str(_ONNX_RUNTIME_DIR))

from arktts_runtime.runtime import ArkTtsRuntime  # noqa: E402
from arktts_runtime.registration import VoiceRegistration  # noqa: E402

# ---- 默认路径配置（可通过环境变量覆盖）----
# 模型默认位于项目根目录 models/（用户已从 onnx_runtime/model 迁移至此）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODEL_DIR = Path(
    os.environ.get(
        "ARKTTS_MODEL_DIR",
        str(_PROJECT_ROOT / "models" / "Audio8-TTS-Preview-0.6B-ONNX-INT4"),
    )
)
DEFAULT_VOICES_DIR = Path(os.environ.get("ARKTTS_VOICES_DIR", str(_ONNX_RUNTIME_DIR / "voices")))
DEFAULT_OUTPUT_DIR = _ONNX_RUNTIME_DIR / "outputs"

_runtime: ArkTtsRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime(threads: int = 5) -> ArkTtsRuntime:
    """获取并缓存 ArkTtsRuntime 单例（模型 session 只加载一次）。"""
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = ArkTtsRuntime(DEFAULT_MODEL_DIR, DEFAULT_VOICES_DIR, threads=threads)
    return _runtime


def list_voices() -> list[dict]:
    """列出已注册的音色。"""
    return get_runtime().voices.list()


def voice_exists(name: str) -> bool:
    """判断音色是否已注册。"""
    return any(v.get("name") == name for v in list_voices())


def register_voice(
    audio_path: str | Path,
    text: str,
    name: str,
    overwrite: bool = False,
) -> dict:
    """从参考音频注册新音色（零样本克隆）。

    Args:
        audio_path: 参考音频文件路径（0.5-30 秒，<=50MiB，wav/mp3/flac 等）。
        text: 参考音频中实际说出的原文，必须与音频内容一致。
        name: 音色名（一个路径分量，<=64 字符）。
        overwrite: 同名音色已存在时是否覆盖。

    Returns:
        音色元信息 dict。
    """
    model_dir = Path(DEFAULT_MODEL_DIR)
    manifest = json.loads((model_dir / "runtime_manifest.json").read_text())
    registration = VoiceRegistration(
        model_dir / "registration",
        DEFAULT_VOICES_DIR,
        manifest["model_fingerprint"],
    )
    data = Path(audio_path).read_bytes()
    return registration.register(data, Path(audio_path).name, text, name, overwrite)


def synthesize(
    text: str,
    voice: str,
    output: str | Path | None = None,
    max_new_tokens: int = 256,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = 50,
    seed: int = 42,
) -> str:
    """合成语音并保存为 WAV，返回保存的音频路径。

    Args:
        text: 待合成的文本（建议不超过 150 字，过长请拆分）。
        voice: 已注册的音色名（先用 register_voice() 注册，或见 list_voices()）。
        output: 输出 WAV 路径；None 时自动生成 outputs/ 下带时间戳的文件。
        max_new_tokens: 最大生成帧数（约 21.5 帧/秒音频）。
        temperature / top_p / top_k: 采样参数，越小越稳定。
        seed: 随机种子，固定可复现。

    Returns:
        str: 保存的 WAV 文件绝对路径。
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    runtime = get_runtime()
    audio, _codes = runtime.synthesize(
        text=text,
        voice=voice,
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        seed=seed,
    )
    if output is None:
        output = DEFAULT_OUTPUT_DIR / f"{voice}_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = int(runtime.manifest["sample_rate"])
    sf.write(str(output), audio, sample_rate)
    return str(output.resolve())


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

    与 synthesize() 推理等价，但音频直接写入 BytesIO，不产生任何本地文件。

    Args:
        同 synthesize()（output 参数除外）。

    Returns:
        tuple[bytes, int]: (WAV 文件字节, 采样率)。
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    runtime = get_runtime()
    audio, _codes = runtime.synthesize(
        text=text,
        voice=voice,
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        seed=seed,
    )
    sample_rate = int(runtime.manifest["sample_rate"])
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV")
    return buf.getvalue(), sample_rate


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
    """流式合成：边生成边返回 16-bit PCM 音频块（惰性生成器）。

    Args:
        text / voice: 同 synthesize()。
        chunk_frames: 每个音频块对应的 codec 帧数（12 帧约 0.56 秒音频）。

    Yields:
        (seq: int, pcm: bytes) —— 单声道 44.1kHz signed 16-bit LE PCM 数据块。
        首个 chunk 之前通过 get_runtime() 拿到采样率；结束由迭代自然终止表示。

    Examples:
        for seq, pcm in iter_pcm_chunks("你好", "zh"):
            play(pcm)   # 边收边播
    """
    runtime = get_runtime()
    for event in runtime.stream(
        text=text,
        voice=voice,
        chunk_frames=chunk_frames,
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        seed=seed,
    ):
        if event["type"] != "audio_chunk":
            continue
        pcm = (np.clip(event["audio"], -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        yield int(event["seq"]), pcm


def stream_synthesize(
    text: str,
    voice: str,
    output: str | Path | None = None,
    chunk_frames: int = 12,
    max_new_tokens: int = 256,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = 50,
    seed: int = 42,
) -> tuple[str, int]:
    """流式合成并落盘 WAV（逐块生成、逐块写盘），返回 (路径, 音频时长秒)。

    与 synthesize() 结果等价，但显存/内存峰值更低、可感知首包延迟更短；
    若要实时播放，直接使用 iter_pcm_chunks()。
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    runtime = get_runtime()
    sample_rate = int(runtime.manifest["sample_rate"])
    if output is None:
        output = DEFAULT_OUTPUT_DIR / f"{voice}_stream_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    frames: list[np.ndarray] = []
    for event in runtime.stream(
        text=text,
        voice=voice,
        chunk_frames=chunk_frames,
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        seed=seed,
    ):
        if event["type"] == "audio_chunk":
            frames.append(event["audio"])
        # complete 事件携带全部 codes，可在此提前结束
    if not frames:
        raise RuntimeError("model produced no audio")
    audio = np.concatenate(frames)
    sf.write(str(output), audio, sample_rate)
    return str(output.resolve()), audio.size / sample_rate


def main() -> None:
    parser = argparse.ArgumentParser(description="Audio8 TTS ONNX 合成")
    parser.add_argument("--text", required=True, help="待合成文本")
    parser.add_argument("--voice", required=True, help="已注册音色名")
    parser.add_argument("--output", type=Path, help="输出 WAV 路径（默认自动生成）")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    path = synthesize(
        text=args.text,
        voice=args.voice,
        output=args.output,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        seed=args.seed,
    )
    print(f"saved {path}")


def main2():
    text = """
Normal synthesis loads only the Slow AR, Fast AR, and codec decoder sessions. On a 16 GB Apple M2 MacBook Air with five ONNX Runtime threads, the service used about 1004 MiB after loading and approximately 1.1-1.2 GiB at synthesis peak. Voice registration unloads those sessions before loading the encoder; the measured registration peak was approximately 1.55 GiB. Measurements vary by operating system and ONNX Runtime allocator behavior.

"""
    # path = synthesize(text, voice="zh")
    path = synthesize(text, voice="en", max_new_tokens=256)
    print(path)


if __name__ == "__main__":
    main2()
