#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fun-CosyVoice3-0.5B-2512 ONNX（Masterx 全 ONNX 导出）CPU 推理测试
=============================================================
中 / 日 / 英 三语零样本合成（cross-lingual 模式：只取参考音频音色，无需参考文本）

- 纯 onnxruntime + numpy + tokenizers 实现，**不需要 torch**（博客方案需要 torch+cosyvoice 框架，
  但本仓库是全 ONNX 导出：LLM/flow/HiFT/编码器均为 .onnx 图 + host loop）
- 模型目录：models/Fun-CosyVoice3-0.5B-2512-ONNX（含上游 campplus/speech_tokenizer/flow.decoder.estimator）
- 推理链路（见仓库 README "Graph contract / Host side"）：
    参考音频 → 16k Whisper log-mel → speech_tokenizer_v3（语音 token，cross-lingual 不用）
             → 16k Kaldi fbank → campplus（说话人嵌入 spks）
    文本 → Qwen2 BPE tokenizer → LLM prompt 组装 → llm_int8 采样（RAS）→ 语音 token
    flow_encoder（token + spks → mu）→ 10 步 CFG flow matching（flow.decoder.estimator）→ mel
    hift 声码器 + iSTFT → 24kHz 音频
"""

import os
import sys
import time
import math
import numpy as np
import onnxruntime as ort
import soundfile as sf
from tokenizers import Tokenizer

MODEL_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "models", "Fun-CosyVoice3-0.5B-2512-ONNX"))
OUT_DIR = os.path.join(os.path.dirname(__file__), "output")

SAMPLE_RATE = 24000          # 输出采样率
LLM_INPUT_SIZE = 896
SPEECH_TOKEN_SIZE = 6561     # FSQ 语音 token 数（0..6560，>=6561 为特殊/停止）
SOS_TOKEN = 6561             # 句子起始
TASK_TOKEN = 6563            # 任务标记

# ---------------- 工具：重采样 / mel 滤波器组 / 特征 ----------------

def resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """线性插值重采样（简单够用）"""
    if src_sr == dst_sr:
        return x
    n_out = int(round(len(x) * dst_sr / src_sr))
    xi = np.linspace(0, len(x) - 1, n_out)
    return np.interp(xi, np.arange(len(x)), x).astype(np.float32)


def _hz_to_mel_slaney(f, fmin=0.0):
    f = np.asarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = (f - fmin) / f_sp
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - fmin) / f_sp
    logstep = np.log(2.0) / 27.0
    mask = f >= min_log_hz
    mels[mask] = min_log_mel + np.log(f[mask] / min_log_hz) / logstep
    return mels


def _mel_to_hz_slaney(m, fmin=0.0):
    m = np.asarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    f = fmin + f_sp * m
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - fmin) / f_sp
    logstep = np.log(2.0) / 27.0
    mask = m >= min_log_mel
    f[mask] = min_log_hz * np.exp(logstep * (m[mask] - min_log_mel))
    return f


def _hz_to_mel_htk(f):
    return 1127.01048 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def mel_filterbank(sr, n_fft, n_mels, fmin=0.0, fmax=None, htk=False):
    """生成 mel 滤波器组 [n_mels, n_fft//2+1]；htk=False 走 Slaney（librosa 默认风格）。"""
    fmax = fmax if fmax is not None else sr / 2
    fft_freqs = np.linspace(0, sr / 2, n_fft // 2 + 1)
    if htk:
        mel_min, mel_max = _hz_to_mel_htk(fmin), _hz_to_mel_htk(fmax)
        mels = np.linspace(mel_min, mel_max, n_mels + 2)
        hz = 700.0 * (np.exp(mels / 1127.01048) - 1.0)
    else:
        mel_min, mel_max = _hz_to_mel_slaney(fmin), _hz_to_mel_slaney(fmax)
        mels = np.linspace(mel_min, mel_max, n_mels + 2)
        hz = _mel_to_hz_slaney(mels, fmin)
    weights = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for i in range(n_mels):
        fl, fc, fr = hz[i], hz[i + 1], hz[i + 2]
        up = (fft_freqs >= fl) & (fft_freqs <= fc)
        if fc > fl:
            weights[i, up] = (fft_freqs[up] - fl) / (fc - fl)
        down = (fft_freqs >= fc) & (fft_freqs <= fr)
        if fr > fc:
            weights[i, down] = (fr - fft_freqs[down]) / (fr - fc)
    if not htk:
        # Slaney 幅度归一化（librosa norm='slaney'）
        sums = weights.sum(axis=1)
        weights = np.where(sums[:, None] > 1.0, weights / sums[:, None], weights)
    return weights.astype(np.float32)


def _stft_power(x, n_fft, hop, periodic=True):
    """功率谱 [n_fft//2+1, T]，hann 窗（periodic），center pad（n_fft//2 两侧反射填充）"""
    win = np.hanning(n_fft + 1)[:-1] if periodic else np.hanning(n_fft)
    x = np.asarray(x, dtype=np.float64)
    pad = n_fft // 2
    xp = np.pad(x, (pad, pad), mode="reflect")
    n_frames = 1 + (len(xp) - n_fft) // hop
    frames = np.stack([xp[i * hop: i * hop + n_fft] for i in range(n_frames)])
    spec = np.fft.rfft(frames * win, n=n_fft, axis=1)          # [T, n_fft//2+1]
    return (np.abs(spec) ** 2).T.astype(np.float32)            # [n_fft//2+1, T]


def whisper_log_mel(audio16k: np.ndarray, n_mels=128):
    """Whisper 风格 log-mel：16k、n_fft 400、hop 160、128 Slaney mel、log10 + 动态范围裁剪"""
    n_fft, hop = 400, 160
    power = _stft_power(audio16k, n_fft, hop)                   # [201, T]
    filters = mel_filterbank(16000, n_fft, n_mels, fmin=0.0, fmax=8000.0, htk=False)
    mel = filters @ power                                        # [128, T]
    log_spec = np.log10(np.clip(mel, 1e-10, None))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = np.minimum(log_spec, log_spec.max() + 4.0)
    return log_spec.astype(np.float32)


def kaldi_fbank(audio16k: np.ndarray, num_mel_bins=80):
    """Kaldi 风格 fbank：16k、preemph 0.97、25ms/10ms、povey 窗、80 HTK mel、log(max(mel,1))、减均值"""
    sr, fl, fs = 16000, 25, 10
    fl_s, fs_s = int(fl * sr / 1000), int(fs * sr / 1000)       # 400 / 160
    x = np.asarray(audio16k, dtype=np.float64)
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])                  # preemphasis
    n_frames = 1 + (len(x) - fl_s) // fs_s
    if n_frames < 1:
        n_frames = 1
    frames = np.stack([x[i * fs_s: i * fs_s + fl_s] for i in range(n_frames)])
    if frames.shape[1] < fl_s:
        frames = np.pad(frames, ((0, 0), (0, fl_s - frames.shape[1])))
    # povey 窗
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(fl_s) / (fl_s - 1))) if fl_s > 1 else np.ones(fl_s)
    spec = np.abs(np.fft.rfft(frames * win, n=fl_s, axis=1)) ** 2   # [T, 201]
    filters = mel_filterbank(sr, fl_s, num_mel_bins, fmin=20.0, fmax=8000.0, htk=True)
    mel = filters @ spec.T                                       # [80, T]
    feat = np.log(np.clip(mel, 1.0, None))                       # Kaldi mel floor 1.0
    feat = feat - feat.mean(axis=1, keepdims=True)               # 每句减均值（mean-normalised）
    return feat.T.astype(np.float32)                             # [T, 80]


def matcha_mel(audio24k: np.ndarray, n_mels=80):
    """CosyVoice Matcha mel：24k、n_fft 1920、hop 480、80 Slaney mel（0..12k）、ln(clamp(1e-5))"""
    n_fft, hop = 1920, 480
    power = _stft_power(audio24k, n_fft, hop)                    # [961, T]
    filters = mel_filterbank(24000, n_fft, n_mels, fmin=0.0, fmax=12000.0, htk=False)
    mel = filters @ power                                         # [80, T]
    return np.log(np.clip(mel, 1e-5, None)).astype(np.float32)


# ---------------- 引擎 ----------------

class CosyVoice3ONNX:
    def __init__(self, model_dir=MODEL_DIR, llm_file="llm_int8.onnx", threads=6):
        self.dir = model_dir
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        prov = ["CPUExecutionProvider"]
        self.text_emb = ort.InferenceSession(os.path.join(model_dir, "text_embedding_fp16.onnx"), opts, prov)
        self.speech_emb = ort.InferenceSession(os.path.join(model_dir, "speech_embedding.onnx"), opts, prov)
        self.llm = ort.InferenceSession(os.path.join(model_dir, llm_file), opts, prov)
        self.flow_enc = ort.InferenceSession(os.path.join(model_dir, "flow_encoder.onnx"), opts, prov)
        self.estimator = ort.InferenceSession(os.path.join(model_dir, "flow.decoder.estimator.fp32.onnx"), opts, prov)
        self.hift = ort.InferenceSession(os.path.join(model_dir, "hift.onnx"), opts, prov)
        self.campplus = ort.InferenceSession(os.path.join(model_dir, "campplus.onnx"), opts, prov)
        self.tokenizer_v3 = ort.InferenceSession(os.path.join(model_dir, "speech_tokenizer_v3.onnx"), opts, prov)
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        # LLM 的 KV cache 输入名（past_key_values.N.key/value）
        self.kv_names = [(f"past_key_values.{i}.key", f"past_key_values.{i}.value") for i in range(24)]
        self.n_layers = 24

    # ---------- 说话人特征 ----------
    def speaker_embedding(self, audio24k: np.ndarray) -> np.ndarray:
        """campplus 说话人嵌入 [1,192]"""
        a16 = resample(audio24k, 24000, 16000)
        fbank = kaldi_fbank(a16)                                  # [T,80]
        emb = self.campplus.run(None, {"input": fbank[None, :, :]})[0]
        return emb.astype(np.float32)                             # [1,192]

    def prompt_mel(self, audio24k: np.ndarray) -> np.ndarray:
        """Matcha mel [80, T]（cross-lingual 不使用，zero-shot 备用）"""
        return matcha_mel(audio24k)

    def prompt_speech_tokens(self, audio24k: np.ndarray) -> np.ndarray:
        """Whisper log-mel → speech_tokenizer_v3 → 语音 token [1,T/4]（cross-lingual 不使用）"""
        a16 = resample(audio24k, 24000, 16000)
        lm = whisper_log_mel(a16)                                 # [128,T]
        tokens = self.tokenizer_v3.run(None, {
            "feats": lm[None, :, :],
            "feats_length": np.array([lm.shape[1]], dtype=np.int32),
        })[0]
        return tokens.astype(np.int64)

    # ---------- 文本 ----------
    def tokenize(self, text: str) -> np.ndarray:
        ids = self.tok.encode(text).ids
        return np.array(ids, dtype=np.int64)

    # ---------- LLM 采样解码（cross-lingual：无需参考文本） ----------
    def _ras_sample(self, logits: np.ndarray, recent: list, top_k=25, top_p=0.8, window=10, tau=0.1):
        logits = logits.astype(np.float64).copy()
        # 重复感知：抑制最近 window 内已采样的 token
        for tok in recent[-window:]:
            logits[tok] *= (1.0 - tau)
        # top-k
        k = min(top_k, logits.shape[0])
        idx = np.argpartition(logits, -k)[-k:]
        keep = np.zeros_like(logits, dtype=bool)
        keep[idx] = True
        logits[~keep] = -np.inf
        # top-p
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        order = np.argsort(probs)[::-1]
        cum = np.cumsum(probs[order])
        cut = order[cum > top_p]
        if cut.size:
            logits[cut[1:]] = -np.inf
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        tok = int(np.random.choice(probs.shape[0], p=probs))
        return tok

    def _empty_kv(self):
        kv = {}
        for k, v in self.kv_names:
            kv[k] = np.zeros((1, 2, 0, 64), dtype=np.float32)
            kv[v] = np.zeros((1, 2, 0, 64), dtype=np.float32)
        return kv

    def _llm_prefill(self, emb: np.ndarray, attn: np.ndarray, pos: np.ndarray, kv: dict):
        feeds = {"inputs_embeds": emb, "attention_mask": attn, "position_ids": pos, **kv}
        outs = self.llm.run(None, feeds)
        logits = outs[0]                                          # [1,6761] 最后位置
        present = {}
        for i in range(self.n_layers):
            present[f"past_key_values.{i}.key"] = outs[1 + i * 2]
            present[f"past_key_values.{i}.value"] = outs[2 + i * 2]
        return logits, present

    def generate_speech_tokens(self, tts_text: str, spk_emb: np.ndarray, max_tokens=600):
        """cross-lingual：text = 'You are a helpful assistant.<|endofprompt|>' + tts_text，无 prompt tokens"""
        prompt_text = "You are a helpful assistant.<|endofprompt|>" + tts_text
        text_ids = self.tokenize(prompt_text)                    # [S]
        n_text = text_ids.shape[0]
        min_len = max(2 * n_text, 1)
        max_len = min(max(20 * n_text, min_len + 10), max_tokens)

        # prefill 输入 emb：speech_emb[sos] ++ text_emb(text_ids) ++ speech_emb[task]
        sos = self.speech_emb.run(None, {"input_ids": np.array([[SOS_TOKEN]], dtype=np.int64)})[0]
        task = self.speech_emb.run(None, {"input_ids": np.array([[TASK_TOKEN]], dtype=np.int64)})[0]
        te = self.text_emb.run(None, {"input_ids": text_ids[None, :]})[0].astype(np.float32)
        emb = np.concatenate([sos, te, task], axis=1)            # [1, 1+S+1, 896]
        L0 = emb.shape[1]
        attn = np.ones((1, L0), dtype=np.int64)
        pos = np.arange(L0, dtype=np.int64)[None, :]
        logits, kv = self._llm_prefill(emb, attn, pos, self._empty_kv())

        tokens: list[int] = []
        recent: list[int] = []
        past = L0
        logits_last = logits[0, :]                               # [6761]
        while True:
            # min 内禁止停止（mask >=6561）
            lg = logits_last.copy()
            if len(tokens) < min_len:
                lg[SPEECH_TOKEN_SIZE:] = -np.inf
            tok = self._ras_sample(lg, recent)
            if tok >= SPEECH_TOKEN_SIZE:
                break
            tokens.append(tok)
            recent.append(tok)
            if len(tokens) >= max_len:
                break
            # 下一步 decode：speech_emb[tok]
            emb_step = self.speech_emb.run(None, {"input_ids": np.array([[tok]], dtype=np.int64)})[0]
            attn_step = np.ones((1, past + 1), dtype=np.int64)
            pos_step = np.array([[past]], dtype=np.int64)
            logits, kvk, kvv = self._llm_decode_step(emb_step, attn_step, pos_step, kv)
            for i in range(self.n_layers):
                kv[f"past_key_values.{i}.key"] = kvk[f"past_key_values.{i}.key"]
                kv[f"past_key_values.{i}.value"] = kvv[f"past_key_values.{i}.value"]
            logits_last = logits[0, :]
            past += 1
            if len(tokens) % 25 == 0:
                print(f"    [llm] {len(tokens)} tokens, last={tokens[-1]}", flush=True)
        print(f"    [llm] done: {len(tokens)} speech tokens")
        return np.array(tokens, dtype=np.int64)

    def _llm_decode_step(self, emb_step: np.ndarray, attn: np.ndarray, pos: np.ndarray, kv: dict):
        feeds = {"inputs_embeds": emb_step, "attention_mask": attn, "position_ids": pos, **kv}
        outs = self.llm.run(None, feeds)
        return outs[0], {
            f"past_key_values.{i}.key": outs[1 + i * 2] for i in range(self.n_layers)
        }, {
            f"past_key_values.{i}.value": outs[2 + i * 2] for i in range(self.n_layers)
        }

    # ---------- Flow matching + 声码 ----------
    def synth_from_tokens(self, tokens: np.ndarray, spk_emb: np.ndarray):
        """flow_encoder + 10 步 CFG flow matching + hift + iSTFT → 24k 音频"""
        t = tokens[None, :]                                      # [1,T]
        mu, spks = self.flow_enc.run(None, {"token": t, "embedding": spk_emb})
        mu = mu.astype(np.float32)                               # [1,80,2T]
        spks = spks.astype(np.float32)                           # [1,80]
        T = tokens.shape[0]
        F = 2 * T                                                # 帧数
        x = np.random.randn(1, 80, F).astype(np.float32)
        cond = np.zeros((1, 80, F), dtype=np.float32)            # cross-lingual：无 prompt mel
        mask = np.ones((2, 1, F), dtype=np.float32)

        # CFG：batch 2 = (cond, zeros)
        mu_b = np.concatenate([mu, np.zeros_like(mu)], axis=0)
        cond_b = np.concatenate([cond, np.zeros_like(cond)], axis=0)
        spks_b = np.concatenate([spks, np.zeros_like(spks)], axis=0)
        x_b = np.concatenate([x, x], axis=0)

        ts = 1.0 - np.cos(np.linspace(0, 1, 11) * np.pi / 2)     # [0..1]
        print("    [flow] Euler 10 steps...", flush=True)
        for i in range(10):
            t_b = np.full(2, ts[i], dtype=np.float32)
            v = self.estimator.run(None, {
                "x": x_b, "mask": mask, "mu": mu_b, "t": t_b, "spks": spks_b, "cond": cond_b,
            })[0]
            v0, v1 = v[0], v[1]
            vv = 1.7 * v0 - 0.7 * v1                              # CFG guidance
            dt = ts[i + 1] - ts[i]
            x = (x + dt * vv).astype(np.float32)
            x_b = np.concatenate([x, x], axis=0)

        speech_feat = x.astype(np.float32)                       # [1,80,F]
        noise = np.random.rand(1, 480 * F, 9).astype(np.float32)
        mag, phase = self.hift.run(None, {"speech_feat": speech_feat, "noise": noise})
        audio = self._istft(mag.astype(np.float32), phase.astype(np.float32))
        return audio

    @staticmethod
    def _istft(mag: np.ndarray, phase: np.ndarray) -> np.ndarray:
        """HiFT 输出 [1,9,N] → iSTFT（n_fft 16, hop 4, Hann periodic, center）→ 音频"""
        n_fft, hop = 16, 4
        spec = (mag[0] * np.exp(1j * phase[0]))                  # [9,N]
        n_frames = spec.shape[1]
        win = np.hanning(n_fft + 1)[:-1].astype(np.float64)
        audio = np.zeros((n_frames - 1) * hop + n_fft)
        win_sq = np.zeros_like(audio)
        for f in range(n_frames):
            xf = np.fft.irfft(spec[:, f], n=n_fft)               # 9 bin → 16 点
            s = f * hop
            audio[s: s + n_fft] += xf * win
            win_sq[s: s + n_fft] += win ** 2
        audio /= np.maximum(win_sq, 1e-10)
        pad = n_fft // 2
        audio = audio[pad: -pad] if audio.shape[0] > 2 * pad else audio
        return np.clip(audio, -0.99, 0.99).astype(np.float32)

    # ---------- 完整合成 ----------
    def synthesize(self, tts_text: str, ref_wav: str) -> np.ndarray:
        print(f"  [ref] {os.path.basename(ref_wav)}")
        audio24k, sr = sf.read(ref_wav, dtype="float32")
        if audio24k.ndim > 1:
            audio24k = audio24k.mean(axis=1)
        if sr != 24000:
            audio24k = resample(audio24k, sr, 24000)
        t0 = time.time()
        spk = self.speaker_embedding(audio24k)
        print(f"  [spk] campplus embedding {spk.shape}  ({time.time()-t0:.1f}s)", flush=True)
        t0 = time.time()
        tokens = self.generate_speech_tokens(tts_text, spk)
        print(f"  [llm] {tokens.shape[0]} tokens in {time.time()-t0:.1f}s", flush=True)
        t0 = time.time()
        audio = self.synth_from_tokens(tokens, spk)
        print(f"  [voc] audio {len(audio)/SAMPLE_RATE:.2f}s in {time.time()-t0:.1f}s", flush=True)
        return audio


# ---------------- 主流程：中 / 日 / 英 三语 ----------------

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    base = MODEL_DIR
    voices = {
        "zh": os.path.join(base, "voices", "zh-female.wav"),
        "en": os.path.join(base, "voices", "en-male.wav"),
        "ja": os.path.join(base, "voices", "zh-female.wav"),   # 无日语音色，用中文音色跨语种合成
    }
    texts = {
        "zh": "你好，欢迎使用豆包语音合成测试。",
        "ja": "こんにちは、これは豆包の音声合成テストです。",
        "en": "Hello, this is a Doubao voice synthesis test.",
    }

    print("加载 ONNX 引擎（CPU）...", flush=True)
    t0 = time.time()
    engine = CosyVoice3ONNX()
    print(f"引擎加载完成（{time.time()-t0:.1f}s）\n", flush=True)

    for lang in ("zh", "ja", "en"):
        print(f"===== {lang.upper()} =====")
        audio = engine.synthesize(texts[lang], voices[lang])
        out = os.path.join(OUT_DIR, f"cosyvoice3_{lang}.wav")
        sf.write(out, audio, SAMPLE_RATE)
        print(f"  已保存 {out}（{len(audio)/SAMPLE_RATE:.2f}s）\n", flush=True)

    print("三语合成完成。输出目录：", OUT_DIR)


if __name__ == "__main__":
    main()
