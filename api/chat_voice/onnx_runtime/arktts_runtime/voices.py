from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def _read_json(path: Path) -> dict:
    """读取 JSON 文件，优先 UTF-8，兼容旧版在 Windows GBK 环境写出的文件。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return json.loads(path.read_text(encoding="gbk"))


class VoiceStore:
    def __init__(self, root: Path, num_codebooks: int):
        self.root = root.resolve()
        self.num_codebooks = int(num_codebooks)

    def list(self) -> list[dict]:
        voices = []
        if not self.root.exists():
            return voices
        for meta_path in sorted(self.root.glob("*/meta.json")):
            try:
                voices.append(_read_json(meta_path))
            except (OSError, json.JSONDecodeError):
                continue
        return voices

    def load(self, name: str) -> tuple[np.ndarray, dict]:
        if not name or Path(name).name != name:
            raise ValueError("invalid voice name")
        voice_dir = self.root / name
        meta_path = voice_dir / "meta.json"
        codes_path = voice_dir / "codes.npy"
        if not meta_path.is_file() or not codes_path.is_file():
            raise KeyError(f"voice not found: {name}")
        meta = _read_json(meta_path)
        codes = np.load(codes_path, allow_pickle=False).astype(np.int64, copy=False)
        if codes.ndim != 2 or codes.shape[0] != self.num_codebooks or codes.shape[1] == 0:
            raise ValueError(f"invalid codes for voice {name}: {codes.shape}")
        reference_text = str(meta.get("reference_text", "")).strip()
        if not reference_text:
            raise ValueError(f"voice {name} has no reference_text")
        return codes, meta
