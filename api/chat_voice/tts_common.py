# -*- coding: utf-8 -*-
"""Shared TTS helpers used by all chat_voice engines (genie_tts / edge_tts / sapi_tts).

detect_language:          lightweight language detection (kana -> ja, hanzi -> zh, else en).
split_text:               sentence-boundary segmentation for long text.
split_language_segments:  split mixed zh/ja/en text into per-language segments so
                          each engine can read every part with the right voice.
"""
import re

# ---- mixed-language segmentation (shared by genie / edge_tts "auto" mode) ----

_EN_RE = re.compile(r"[A-Za-z\uFF21-\uFF3A\uFF41-\uFF5A]")
_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u31F0-\u31FF\uFF66-\uFF9D]")
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_STRONG_PUNCT = set("。！？!?…")
# clause breaks separate a hanzi run from a neighbouring kana word so the hanzi
# is not mistaken for Japanese (strong + soft CJK punctuation + newline)
_CLAUSE_BREAK = set("。！？!?…，、；：,;:\n")


def _classify(ch: str) -> str:
    if _EN_RE.match(ch):
        return "en"
    if _KANA_RE.match(ch):
        return "kana"
    if _CJK_RE.match(ch):
        return "cjk"
    return "neutral"


def split_language_segments(text: str, merge_en_with_cjk: bool = False) -> list[tuple[str, str]]:
    """Split mixed text into [(lang, text), ...], lang in {'zh','ja','en'}.

    Heuristics: kana -> ja; hanzi -> ja when its semantic block (bounded by
    English runs / strong punctuation) contains kana, else zh; latin -> en.
    merge_en_with_cjk=True folds adjacent (unpunctuated) English back into the
    neighbouring zh/ja segment instead of reading it with an English voice.
    """
    if not text or not text.strip():
        return []

    runs = []  # (class, chars, punct_pref, punct_suff)
    for ch in text:
        cls = _classify(ch)
        if runs and runs[-1][0] == cls and not runs[-1][2] and not runs[-1][3]:
            runs[-1] = (cls, runs[-1][1] + ch, False, False)
        else:
            runs.append((cls, ch, False, False))

    merged = []
    for cls, chars, _, _ in runs:
        if cls == "neutral":
            if merged:
                c, t, pf, sf_ = merged[-1]
                merged[-1] = (c, t + chars, pf, True)
            else:
                merged.append((cls, chars, False, False))
        else:
            merged.append((cls, chars, False, False))

    if merged and merged[0][0] == "neutral":
        _cls, chars, _, _ = merged[0]
        if len(merged) >= 2:
            c2, t2, _, sf2 = merged[1]
            merged[1] = (c2, chars + t2, True, sf2)
            merged.pop(0)
        else:
            return []

    n = len(merged)
    bounds = [False] * (n + 1)
    for i in range(n - 1):
        cls_l, t_l, _, _ = merged[i]
        cls_r, _, _, _ = merged[i + 1]
        if cls_l == "en" or cls_r == "en":
            bounds[i + 1] = True
        if t_l and t_l[-1] in _STRONG_PUNCT:
            bounds[i + 1] = True

    langs: list[str | None] = [None] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and not bounds[j + 1]:
            j += 1
        # Hanzi belongs to Japanese only when the nearest kana neighbour inside
        # the block is closer than any sentence break, so a Chinese clause that
        # merely sits next to a Japanese word (e.g. "...礼貌程度\n- こんにちは")
        # is still read as Chinese instead of the whole block flipping to ja.
        kana_idx = [k for k in range(i, j + 1) if merged[k][0] == "kana"]
        for k in range(i, j + 1):
            cls = merged[k][0]
            if cls == "kana":
                langs[k] = "ja"
            elif cls == "en":
                langs[k] = "en"
            elif cls == "cjk":
                if not kana_idx:
                    langs[k] = "zh"
                    continue
                nearest = min(kana_idx, key=lambda ki: abs(ki - k))
                lo, hi = (k, nearest) if k < nearest else (nearest, k)
                # a clause break anywhere between this hanzi run and the nearest
                # kana (punctuation/newline is folded into a run's text, so scan
                # the full span text, not just run-final chars) means they are
                # different clauses -> read the hanzi as Chinese. Hanzi chars
                # themselves are never breaks, so including the runs is harmless.
                span = "".join(merged[m][1] for m in range(lo, hi + 1))
                broken = any(ch in _CLAUSE_BREAK for ch in span)
                langs[k] = "zh" if broken else "ja"
        i = j + 1

    changed = merge_en_with_cjk
    while changed:
        changed = False
        for i in range(n):
            if langs[i] != "en":
                continue
            _cls, chars, pf, sf_ = merged[i]
            if i > 0 and not merged[i - 1][3] and not pf and langs[i - 1] != "en":
                c2, t2, pf2, _sf2 = merged[i - 1]
                merged[i - 1] = (c2, t2 + chars, pf2, True)
                merged.pop(i)
                langs.pop(i)
                n -= 1
                changed = True
                break
            if i + 1 < n and not sf_ and not merged[i + 1][2] and langs[i + 1] != "en":
                c2, t2, _pf2, sf2 = merged[i + 1]
                merged[i + 1] = (c2, chars + t2, True, sf2)
                merged.pop(i)
                langs.pop(i)
                n -= 1
                changed = True
                break

    segments: list[tuple[str, str]] = []
    for i in range(n):
        lang, (_cls, chars, _, _) = langs[i], merged[i]
        if segments and segments[-1][0] == lang:
            segments[-1] = (lang, segments[-1][1] + chars)
        else:
            segments.append((lang or "zh", chars))
    return [(lang, t.strip()) for lang, t in segments if t.strip()]


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
    print("merged", merged)
    return merged or [text]
