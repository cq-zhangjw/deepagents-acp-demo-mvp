"""AI 语音合成 API（子路由，Audio8-TTS 版）。

- 语音输入（STT）：浏览器 Web Speech Recognition（webkitSpeechRecognition），前端完成，识别文字填入主聊天输入框。
- 语音输出（TTS）：本模块调用 Audio8 TTS（ONNX Runtime 纯 CPU，模型位于根目录 models/），内存合成 WAV，不落盘。

入口：app.py include_router（前缀 /api/chat_voice）
"""

import base64
import logging
import os

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .tts_api import iter_pcm_chunks, list_voices, split_text, synthesize_wav_bytes, voice_exists
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat_voice", tags=["chat_voice"])

# 音色名 → 前端展示标签（Audio8 注册音色：zh / en）
_VOICE_LABELS = {
    "zh": "中文",
    "en": "English",
}

# 播报模式：TTS_MODE=stream（流式，首包低延迟）| file（非流式，等完整 WAV 后播放）
# 流式每块音频帧数：TTS_STREAM_CHUNK_FRAMES（越大首包延迟越高、全量解码开销越小，CPU 机器建议 48+）
_SAMPLE_RATE = 44100


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
    """将 Audio8 已注册音色映射为前端 {label, value} 格式。"""
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
    text: str = Field(..., description="待朗读文本")
    voice: str = Field(default="zh", description="TTS 音色名（Audio8 注册音色）")


class TTSResponse(BaseModel):
    text: str = Field(..., description="回显文本")
    audio: str = Field(..., description="TTS WAV base64（合成失败时为空字符串）")


@router.get("/voices")
def get_voices():
    """返回 Audio8 已注册的音色列表。"""
    return _voice_list()


@router.get("/config")
def get_config():
    """返回播报模式配置，前端据此选择流式 / 非流式播放路径。"""
    return {
        "mode": _tts_mode(),
        "chunk_frames": _stream_chunk_frames(),
        "sample_rate": _SAMPLE_RATE,
    }


@router.post("/tts", response_model=TTSResponse)
def tts(req: TTSRequest):
    """纯 TTS：将指定文本合成为 WAV base64（内存合成，不落盘、不调用 LLM）。"""
    audio = _synthesize(req.text, req.voice or "zh")
    return TTSResponse(text=req.text, audio=base64.b64encode(audio).decode())


@router.post("/tts_stream")
def tts_stream(req: TTSRequest):
    """流式 TTS：边合成边返回 16-bit PCM（单声道 44.1kHz LE），降低首包延迟。

    长文本按句子分段，逐段流式合成并 yield，前端收到连续的 PCM 字节流；
    失败时流提前结束。
    """
    def generate():
        if not req.text.strip():
            return
        try:
            voice = req.voice or "zh"
            if not voice_exists(voice):
                voice = "zh"
            for seg in split_text(req.text):
                for _seq, pcm in iter_pcm_chunks(seg, voice=voice, max_new_tokens=512, chunk_frames=_stream_chunk_frames()):
                    yield pcm
        except Exception as exc:  # noqa: BLE001
            logger.error("audio8 tts stream failed: %s", exc)

    return StreamingResponse(generate(), media_type="application/octet-stream")


def _synthesize(text: str, voice: str) -> bytes:
    if not text:
        return b""
    try:
        if not voice or not voice_exists(voice):
            voice = "zh"
        wav_bytes, _sample_rate = synthesize_wav_bytes(text, voice=voice, max_new_tokens=512)
        return wav_bytes
    except Exception as exc:  # noqa: BLE001
        logger.error("audio8 tts synthesis failed: %s", exc)
        return b""
