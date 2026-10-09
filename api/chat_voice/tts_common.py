# -*- coding: utf-8 -*-
"""Shared TTS helpers used by all chat_voice engines (genie_tts / edge_tts / sapi_tts).

detect_language: lightweight language detection (kana -> ja, hanzi -> zh, else en).
split_text:     sentence-boundary segmentation for long text.
"""
import re


def detect_language(text: str) -> str:
    """Lightweight language detection: kana -> ja; CJK hanzi -> zh; otherwise en."""
    if not text:
        return "en"
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff\uff66-\uff9f]", text):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en"


def split_text(text: str, max_chars: int = 120) -> list[str]:
    """Split long text into segments on sentence boundaries to stay within the
    engine's per-request limits.

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
