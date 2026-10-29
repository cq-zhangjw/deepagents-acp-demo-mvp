
import json
import os
from pathlib import Path

from langchain.chat_models import init_chat_model
from dotenv import load_dotenv

MODE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mode_config.json"


def _load_configs() -> list[dict]:
    try:
        with open(MODE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _active_config() -> dict | None:
    configs = _load_configs()
    for cfg in configs:
        if cfg.get("active"):
            return cfg
    return configs[0] if configs else None


def current_model_name() -> str:
    cfg = _active_config()
    return (cfg or {}).get("model_name") or "qwen3.8-9b"


def current_content_size() -> int:
    """当前（active）模型的上下文窗口大小（tokens），前端上下文比例总数与压缩阈值都以它为准。"""
    cfg = _active_config()
    try:
        return int((cfg or {}).get("content_size") or 0)
    except (TypeError, ValueError):
        return 0


def list_models() -> list[dict]:
    return _load_configs()


def _load_model_config(model_name: str) -> dict:
    configs = _load_configs()
    for cfg in configs:
        if cfg.get("model_name") == model_name:
            return cfg
    available = ", ".join(c.get("model_name", "?") for c in configs)
    raise ValueError(
        f"MODEL_NAME '{model_name}' not found in {MODE_CONFIG_PATH.name}. Available: {available}"
    )


def create_model(model_name: str | None = None):
    """Build a chat model; reloads mode_config.json on every call."""
    load_dotenv(override=True)

    model_name = model_name or current_model_name()
    timeout = float(os.getenv("TIMEOUT")) if os.getenv("TIMEOUT") else None
    max_retry = int(os.getenv("MAX_RETRY")) if os.getenv("MAX_RETRY") else 3

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
