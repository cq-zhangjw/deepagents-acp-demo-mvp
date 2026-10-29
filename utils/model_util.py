
import json
import os
from pathlib import Path

from langchain.chat_models import init_chat_model
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_CONFIG_PATH = BASE_DIR / "model_config.json"
MODE_CONFIG_PATH = BASE_DIR / "mode_config.json"

DEFAULT_MODEL = "qwen3.8-9b"


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_mode_config() -> dict:
    """运行模式配置（mode_config.json）：content_size / 当前模型 / timeout / max_retry。"""
    cfg = _load_json(MODE_CONFIG_PATH, {})
    return cfg if isinstance(cfg, dict) else {}


def current_model_name() -> str:
    cfg = load_mode_config()
    return cfg.get("model") or DEFAULT_MODEL


def list_models() -> list[dict]:
    """模型配置列表（model_config.json），供前端模型选择器使用。"""
    configs = _load_json(MODEL_CONFIG_PATH, [])
    return configs if isinstance(configs, list) else []


def _load_model_config(model_name: str) -> dict:
    configs = list_models()
    for cfg in configs:
        if cfg.get("model_name") == model_name:
            return cfg
    available = ", ".join(c.get("model_name", "?") for c in configs)
    raise ValueError(
        f"MODEL_NAME '{model_name}' not found in {MODEL_CONFIG_PATH.name}. Available: {available}"
    )


def create_model(model_name: str | None = None):
    """Build a chat model; reloads mode_config.json / model_config.json on every call."""
    load_dotenv(override=True)

    model_name = model_name or current_model_name()
    mode_cfg = load_mode_config()
    timeout = float(mode_cfg["timeout"]) if mode_cfg.get("timeout") else None
    max_retry = int(mode_cfg["max_retry"]) if mode_cfg.get("max_retry") else 3

    cfg = _load_model_config(model_name)
    top_p = cfg.get("top_p")

    kwargs = dict(
        model=model_name,
        model_provider=cfg.get("model_provider") or "openai",
        base_url=cfg.get("endpoint") or "http://127.0.0.1:8088",
        api_key=cfg.get("api_key") or os.getenv("API_KEY") or None,
        timeout=timeout,
        max_retries=max_retry,
        temperature=float(cfg["temperature"]) if cfg.get("temperature") is not None else 0.5,
        max_tokens=int(cfg["max_tokens"]) if cfg.get("max_tokens") is not None else 65535,
    )
    if top_p is not None:
        kwargs["top_p"] = float(top_p)

    return init_chat_model(**kwargs)
