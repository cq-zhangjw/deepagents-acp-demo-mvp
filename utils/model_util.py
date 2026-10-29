import json
import os
from pathlib import Path

from langchain.chat_models import init_chat_model
from dotenv import load_dotenv

MODEL_CONFIG_PATH = Path(__file__).resolve().parent.parent / "model_config.json"


def _load_configs() -> list[dict]:
    try:
        with open(MODEL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def current_model_name() -> str:
    """模型名由前端选择并经 WS 查询参数注入为环境变量 MODEL_NAME（参考 AGENT_NAME 的传递）。

    未注入时回退到 model_config.json 的第一个模型。
    """
    name = os.getenv("MODEL_NAME", "").strip()
    if name:
        return name
    configs = _load_configs()
    return configs[0].get("model_name") if configs else ""


def current_content_size() -> int:
    """当前模型的上下文窗口大小（tokens），前端上下文比例总数与压缩阈值都以它为准。"""
    cfg = get_model_config(current_model_name(), raise_on_missing=False)
    try:
        return int((cfg or {}).get("content_size") or 0)
    except (TypeError, ValueError):
        return 0


def list_models() -> list[dict]:
    return _load_configs()


def get_model_config(model_name: str, raise_on_missing: bool = True) -> dict:
    configs = _load_configs()
    for cfg in configs:
        if cfg.get("model_name") == model_name:
            return cfg
    if raise_on_missing:
        available = ", ".join(c.get("model_name", "?") for c in configs)
        raise ValueError(
            f"MODEL_NAME '{model_name}' not found in {MODEL_CONFIG_PATH.name}. Available: {available}"
        )
    return {}


def create_model(model_name: str | None = None):
    """Build a chat model; reloads model_config.json on every call.

    The model name follows the frontend selection (WS query param -> env
    MODEL_NAME), same pattern as AGENT_NAME.
    """
    load_dotenv(override=True)

    model_name = model_name or current_model_name()
    timeout = float(os.getenv("TIMEOUT")) if os.getenv("TIMEOUT") else None
    max_retry = int(os.getenv("MAX_RETRY")) if os.getenv("MAX_RETRY") else 3

    cfg = get_model_config(model_name)
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
