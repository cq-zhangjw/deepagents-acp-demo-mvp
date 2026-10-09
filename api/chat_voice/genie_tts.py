# -*- coding: utf-8 -*-
"""
中日英混合文本 → 拆句 → 分角色合成 → 合并音频

用法:
    python mixed_tts.py                # 运行内置演示
    # 或在你的代码里:
    from mixed_tts import mixed_synthesize
    result = mixed_synthesize("你好，Hello，こんにちは。我是菲比，很高兴认识你。", "output/out.wav")

拆句规则 (启发式):
    - 英文字母 → English 片段 (三十seven)
    - 平假名/片假名 → Japanese 片段 (未花)
    - 汉字 → 若同属一个"语义块"(以强标点/英文为界) 内出现假名, 视为日语汉字, 否则视为中文
    - 标点/空格/数字归并到相邻片段; 相邻片段间无标点时英文并回中文/日文段(交给引擎的 hybrid 或 pyopenjtalk)
    - 合成时各语言片段用对应角色模型, 片段之间可插入停顿, 最后合并为一个 WAV
"""
import os
import re
import json
import uuid
import shutil
import subprocess
import tempfile

import numpy as np
import soundfile as sf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))            # api/chat_voice
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))        # project root
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
MODELS_DIR = os.path.join(PROJECT_ROOT, "models", "genie-tts-models", "models")
REF_DIR = os.path.join(PROJECT_ROOT, "models", "reference_audio")

# Must be set before importing the genie_tts pip package
os.environ["GENIE_DATA_DIR"] = os.getenv(
    "GENIE_DATA_DIR", os.path.join(PROJECT_ROOT, "models", "xnnehanglab-geniedata")
)

import genie_tts as genie

SAMPLE_RATE = 32000

# voice options exposed to the frontend: zh / en / jp / auto (mixed output)
_VOICE_OPTIONS = [
    ("zh", "Chinese"),
    ("en", "English"),
    ("jp", "Japanese"),
    ("auto", "Auto (mixed)"),
]

# 各语言 → 角色配置 (模型目录 + 官方参考音频)
LANG_CONFIG = {
    "zh": {"character": "feibi",       "language": "Chinese",  "ref": "feibi"},
    "jp": {"character": "mika",        "language": "Japanese", "ref": "mika"},
    "en": {"character": "thirtyseven", "language": "English",  "ref": "thirtyseven"},
}

_char_cache = {}

# ---------------- 1. 拆句 ----------------

_EN_RE = re.compile(r"[A-Za-z\uFF21-\uFF3A\uFF41-\uFF5A]")
_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u31F0-\u31FF\uFF66-\uFF9D]")
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_STRONG_PUNCT = set("。！？!?…")


def _classify(ch: str) -> str:
    if _EN_RE.match(ch):
        return "en"
    if _KANA_RE.match(ch):
        return "kana"
    if _CJK_RE.match(ch):
        return "cjk"
    return "neutral"


def split_language_segments(text: str, merge_en_with_cjk: bool = False):
    """
    将混合文本拆成 [(lang, text), ...], lang ∈ {'zh','jp','en'}。

    merge_en_with_cjk: 默认 False = 严格按语言切分, "私はHelloと言います" 拆成
    jp/en/jp 三段, Hello 由英语角色读; 设为 True 则把紧贴(无标点)的英文并入
    相邻日/中段, 交给 pyopenjtalk / 中文 hybrid 按本语言读。
    """
    if not text or not text.strip():
        return []

    # 1) 按字符类别切成连续 run
    runs = []  # (class, chars, punct_pref, punct_suff)
    for ch in text:
        cls = _classify(ch)
        if runs and runs[-1][0] == cls and not runs[-1][2] and not runs[-1][3]:
            runs[-1] = (cls, runs[-1][1] + ch, False, False)
        else:
            runs.append((cls, ch, False, False))

    # 2) 标点/空格/数字归并到相邻 run
    merged = []
    for cls, chars, _, _ in runs:
        if cls == "neutral":
            if merged:
                c, t, pf, sf_ = merged[-1]
                merged[-1] = (c, t + chars, pf, True)  # 标记尾部有标点
            else:
                if merged and False:
                    pass
                # 文本开头的标点归到下一个 run
                merged.append((cls, chars, False, False))
        else:
            merged.append((cls, chars, False, False))

    # 处理开头 neutral: 把它并入紧随其后的 run
    if merged and merged[0][0] == "neutral":
        cls, chars, _, _ = merged[0]
        if len(merged) >= 2:
            c2, t2, _, sf2 = merged[1]
            merged[1] = (c2, chars + t2, True, sf2)
            merged.pop(0)
        else:
            return []

    # 3) 判断汉字归属: 以英文 run 和"以强标点结尾的 run"为界切分语义块,
    #    块内出现假名 → 块内汉字按日语读, 否则按中文读
    #    先定界
    n = len(merged)
    bounds = [False] * (n + 1)  # bounds[i] 表示 merged[i] 与 merged[i+1] 之间是否切断
    for i in range(n - 1):
        cls_l, t_l, _, _ = merged[i]
        cls_r, _, _, _ = merged[i + 1]
        if cls_l == "en" or cls_r == "en":
            bounds[i + 1] = True
        if t_l and t_l[-1] in _STRONG_PUNCT:
            bounds[i + 1] = True

    langs = [None] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and not bounds[j + 1]:
            j += 1
        block_has_kana = any(merged[k][0] == "kana" for k in range(i, j + 1))
        for k in range(i, j + 1):
            cls = merged[k][0]
            if cls == "kana":
                langs[k] = "jp"
            elif cls == "cjk":
                langs[k] = "jp" if block_has_kana else "zh"
            elif cls == "en":
                langs[k] = "en"
        i = j + 1

    # 4) 可选: 紧贴(无标点分隔)的英文并回相邻非英文段
    #    默认 False: 严格按语言切分 (Hello 由英语角色读)
    changed = merge_en_with_cjk
    while changed:
        changed = False
        for i in range(n):
            if langs[i] != "en":
                continue
            cls, chars, pf, sf_ = merged[i]
            # 找紧贴的前后邻居 (无标点)
            if i > 0 and not merged[i - 1][3] and not pf and langs[i - 1] != "en":
                c2, t2, pf2, sf2 = merged[i - 1]
                merged[i - 1] = (c2, t2 + chars, pf2, True)
                langs[i - 1] = langs[i - 1]
                merged.pop(i)
                langs.pop(i)
                n -= 1
                changed = True
                break
            if i + 1 < n and not sf_ and not merged[i + 1][2] and langs[i + 1] != "en":
                c2, t2, pf2, sf2 = merged[i + 1]
                merged[i + 1] = (c2, chars + t2, True, sf2)
                langs[i + 1] = langs[i + 1]
                merged.pop(i)
                langs.pop(i)
                n -= 1
                changed = True
                break

    # 5) 合并相邻同语言段
    segments = []
    for i in range(n):
        lang, (cls, chars, _, _) = langs[i], merged[i]
        if segments and segments[-1][0] == lang:
            segments[-1] = (lang, segments[-1][1] + chars)
        else:
            segments.append((lang, chars))
    return [(lang, t.strip()) for lang, t in segments if t.strip()]


# ---------------- 2. 分段合成 ----------------

def _get_character(lang: str):
    cfg = LANG_CONFIG[lang]
    name = cfg["character"]
    if name in _char_cache:
        return name
    model_dir = os.path.join(MODELS_DIR, name)
    genie.load_character(character_name=name, onnx_model_dir=model_dir, language=cfg["language"])
    with open(os.path.join(REF_DIR, name, "prompt_wav.json"), "r", encoding="utf-8") as f:
        prompt = json.load(f)["Normal"]
    genie.set_reference_audio(
        character_name=name,
        audio_path=os.path.join(REF_DIR, name, "prompt_wav", prompt["wav"]),
        audio_text=prompt["text"],
    )
    _char_cache[name] = True
    return name


def _synth_segment(lang: str, text: str, tmp_dir: str) -> np.ndarray:
    name = _get_character(lang)
    tmp_wav = os.path.join(tmp_dir, f"seg_{lang}_{uuid.uuid4().hex[:8]}.wav")
    genie.tts(
        character_name=name,
        text=text,
        play=False,
        split_sentence=True,
        save_path=tmp_wav,
    )
    audio, sr = sf.read(tmp_wav, dtype="float32")
    os.remove(tmp_wav)
    return audio


# ---------------- 3. 语速控制 (后期保音高变速) ----------------

# ffmpeg 查找顺序: 显式路径 > 环境变量 FFMPEG_BIN > PATH > 已知位置
_FFMPEG_KNOWN = [
    r"F:\2023\ZHANGJW\Company Projects\FRDC\tools3\smart-ratial-gpt\ffmpeg-6.0-full_build\bin\ffmpeg.exe",
]


def find_ffmpeg(ffmpeg_path=None):
    if ffmpeg_path and os.path.isfile(ffmpeg_path):
        return ffmpeg_path
    env_bin = os.environ.get("FFMPEG_BIN")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    which = shutil.which("ffmpeg")
    if which:
        return which
    for p in _FFMPEG_KNOWN:
        if os.path.isfile(p):
            return p
    return None


def apply_speed(input_wav: str, speed: float, output_wav: str, ffmpeg_path=None):
    """
    对 wav 做保音高变速 (ffmpeg atempo 滤镜, 支持 0.5x~2x 之外的倍数自动拆链)。

    speed > 1 加快, < 1 减慢, = 1 不变。
    """
    if abs(speed - 1.0) < 1e-6:
        shutil.copy2(input_wav, output_wav)
        return output_wav
    if speed <= 0:
        raise ValueError("speed 必须 > 0")

    ffmpeg = find_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg, 请设置 FFMPEG_BIN 环境变量或传入 ffmpeg_path")

    # atempo 单步只支持 [0.5, 2.0], 超出则分解为 n 个等比例因子
    n = max(1, int(abs(np.log2(speed))) + 1)
    factor = speed ** (1.0 / n)
    filters = ",".join(f"atempo={factor:.6f}" for _ in range(n))
    cmd = [ffmpeg, "-y", "-i", input_wav, "-filter:a", filters,
           "-ar", str(SAMPLE_RATE), "-ac", "1", output_wav]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 变速失败: {proc.stderr[-500:]}")
    return output_wav


# ---------------- 4. 合并 ----------------

def mixed_synthesize(text: str, output_path=None, gap: float = 0.25, speed: float = 1.0, voice: str = "auto"):
    """
    中日英混合文本 → 合并音频。

    Args:
        text: 混合文本
        output_path: 输出 wav 路径 (默认 output/tts_mixed_merged.wav)
        gap: 不同语言片段之间的停顿秒数 (默认 0.25s)
        speed: 语速倍数, 1.0 原速; 1.3 加快 30%; 0.8 放慢 20%
               原生不支持语速, 此处为 ffmpeg atempo 后期保音高变速
        voice: "auto" 按拆句结果混合角色合成; "zh"/"en"/"jp" 强制单语言角色

    Returns:
        {"path": str, "segments": [(lang, text, duration_sec), ...], "sr": int,
         "speed": float, "orig_duration": float}
    """
    if output_path is None:
        output_path = os.path.join(OUTPUT_DIR, "tts_mixed_merged.wav")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    segments = split_language_segments(text)
    if not segments:
        # 全部为标点/数字等, 兜底按中文处理
        segments = [("zh", text)]
    if voice in ("zh", "en", "jp"):
        # 单语言模式: 全部片段用所选语言的角色朗读
        segments = [(voice, seg_text) for _, seg_text in segments]

    seg_info = []
    parts = []
    with tempfile.TemporaryDirectory(dir=os.path.dirname(output_path) or BASE_DIR) as tmp_dir:
        for lang, seg_text in segments:
            audio = _synth_segment(lang, seg_text, tmp_dir)
            parts.append(audio)
            seg_info.append((lang, seg_text, float(len(audio)) / SAMPLE_RATE))
            if gap > 0:
                parts.append(np.zeros(int(SAMPLE_RATE * gap), dtype="float32"))

    merged = np.concatenate(parts) if parts else np.zeros(0, dtype="float32")
    orig_duration = float(len(merged)) / SAMPLE_RATE
    sf.write(output_path, merged, SAMPLE_RATE)

    if abs(speed - 1.0) > 1e-6:
        apply_speed(output_path, speed, output_path + ".tmp.wav")
        os.replace(output_path + ".tmp.wav", output_path)

    return {
        "path": output_path,
        "segments": seg_info,
        "sr": SAMPLE_RATE,
        "speed": speed,
        "orig_duration": orig_duration,
    }


# ---------------- 引擎契约接口 (chat_voice.py 调用) ----------------

def list_voices() -> list[dict]:
    """Return the preset voice options: zh / en / jp / auto."""
    return [{"name": name, "label": label} for name, label in _VOICE_OPTIONS]


def resolve_voice(text: str, voice: str) -> str:
    """Return a valid voice option; unknown/empty falls back to auto (mixed)."""
    v = (voice or "").strip()
    return v if v in ("zh", "en", "jp", "auto") else "auto"


def synthesize_wav_bytes(text: str, voice: str = "auto") -> tuple[bytes, int]:
    """Synthesize text into WAV bytes (in-memory; no file kept)."""
    voice = resolve_voice(text, voice)
    if not text.strip():
        return b"", SAMPLE_RATE
    with tempfile.TemporaryDirectory() as tmp_dir:
        out = os.path.join(tmp_dir, "genie_out.wav")
        mixed_synthesize(text, output_path=out, gap=0.25, speed=1.0, voice=voice)
        with open(out, "rb") as f:
            return f.read(), SAMPLE_RATE


def iter_pcm_chunks(text: str, voice: str = "auto", gap: float = 0.25):
    """Segment-level streaming: synthesize each language segment and yield
    its 16-bit PCM bytes immediately, so the first packet lands after the
    first segment finishes (not after the whole text).

    Segments are produced by split_language_segments (mixed) or forced to
    the selected single language; a short silence gap keeps the rhythm.
    """
    voice = resolve_voice(text, voice)
    if not text.strip():
        return
    segments = split_language_segments(text)
    if not segments:
        segments = [("zh", text)]
    if voice in ("zh", "en", "jp"):
        segments = [(voice, seg_text) for _, seg_text in segments]
    silence = np.zeros(int(SAMPLE_RATE * gap), dtype="float32")
    with tempfile.TemporaryDirectory() as tmp_dir:
        for idx, (lang, seg_text) in enumerate(segments):
            audio = _synth_segment(lang, seg_text, tmp_dir)
            if gap > 0 and idx < len(segments) - 1:
                audio = np.concatenate([audio, silence])
            pcm = (audio * 32767.0).astype(np.int16).tobytes()
            yield 0, pcm


# ---------------- 演示 ----------------

if __name__ == "__main__":
    samples = [
        "你好，Hello，こんにちは。我是菲比，很高兴认识你。Nice to meet you. よろしくお願いします。",
        "私はHelloと言います。",
        "今天天气不错，我们去公园散步吧。",
        "It is a sunny day, let us go to the park.",
        "CPU测试，内存测试，全部通过。",
    ]
    print("===== 拆句结果预览 =====")
    for s in samples:
        segs = split_language_segments(s)
        print(f"文本: {s}")
        for lang, t in segs:
            print(f"  [{lang}] {t}")
        print()

    demo_text = samples[0]
    print("===== 正式合成 =====")
    print("文本:", demo_text)
    result = mixed_synthesize(demo_text, gap=0.25, speed=1.0)
    print(f"输出: {result['path']}")
    print(f"片段数: {len(result['segments'])}")
    for i, (lang, t, dur) in enumerate(result["segments"], 1):
        print(f"  {i}. [{lang}] ({dur:.2f}s) {t}")

    print("\n===== 语速控制测试 (ffmpeg atempo 保音高变速) =====")
    for sp, suffix in [(1.3, "1_3x"), (0.8, "0_8x")]:
        out = os.path.join(OUTPUT_DIR, f"tts_mixed_speed_{suffix}.wav")
        apply_speed(result["path"], sp, out)
        info = sf.info(out)
        print(f"  {sp}x -> {out} ({info.duration:.2f}s)")

    print("\n===== 回归用例: 日语句内嵌英文 =====")
    t2 = "私はHelloと言います。"
    segs2 = split_language_segments(t2)
    print("拆句:", [(l, s) for l, s in segs2])
    r2 = mixed_synthesize(
        t2,
        output_path=os.path.join(OUTPUT_DIR, "tts_mixed_jp_en_hello.wav"),
        gap=0.2,
    )
    for i, (lang, t, dur) in enumerate(r2["segments"], 1):
        print(f"  {i}. [{lang}] ({dur:.2f}s) {t}")
    print(f"输出: {r2['path']}")
    print("🎉 完成")
