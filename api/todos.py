"""Task step checklist (todo) storage for the ACP agent.

Each todo lives in `db/todos/<todo_id>.json`, independent from conversation
history so context compaction never touches it. The agent maintains the list
through the create_todo / update_todo / get_todo builtin tools (defined in
acp_agent.py); the frontend renders the checklist from ACP tool-call events
and restores it via the read-only GET /api/todos/{todo_id}.
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/todos", tags=["todos"])

logger = logging.getLogger("api.todos")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()

STATUSES = ("pending", "in_progress", "completed", "blocked")


def _root_dir() -> Path:
    raw = os.getenv("TODOS_ROOT", "").strip()
    if raw:
        root = Path(raw)
        if not root.is_absolute():
            root = _PROJECT_ROOT / root
        return root
    root = _PROJECT_ROOT / "db" / "todos"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _write(todo: dict[str, Any]) -> None:
    with _LOCK:
        path = _root_dir() / f"{todo['todo_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(todo, f, ensure_ascii=False, indent=2)


def create_todo(items: list[dict[str, Any]], session_id: str | None = None) -> dict[str, Any]:
    norm: list[dict[str, Any]] = []
    for i, item in enumerate(items or []):
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            continue
        norm.append({
            "index": i,
            "title": str(item.get("title", "")).strip(),
            "description": str(item.get("description", "") or "").strip() or None,
            "status": "pending",
            "note": None,
            "updatedAt": _now_ms(),
        })
    if not norm:
        raise ValueError("create_todo requires at least one non-empty step")
    todo_id = f"t_{uuid.uuid4().hex[:8]}"
    todo = {
        "todo_id": todo_id,
        "session_id": session_id,
        "items": norm,
        "createdAt": _now_ms(),
        "updatedAt": _now_ms(),
        "completedAt": None,
    }
    _write(todo)
    return todo


def update_todo(todo_id: str, step_index: int, status: str, note: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    todo = get_todo(todo_id)
    if todo is None:
        raise KeyError(f"todo not found: {todo_id}")
    items = todo["items"]
    if not isinstance(step_index, int) or not (0 <= step_index < len(items)):
        raise ValueError(f"step_index out of range: {step_index}")
    items[step_index]["status"] = status
    items[step_index]["note"] = (str(note or "").strip() or None)
    items[step_index]["updatedAt"] = _now_ms()
    todo["updatedAt"] = _now_ms()
    if status == "completed" and all(it.get("status") == "completed" for it in items):
        todo["completedAt"] = _now_ms()
    else:
        todo["completedAt"] = None
    _write(todo)
    return todo


def get_todo(todo_id: str) -> dict[str, Any] | None:
    if not todo_id:
        return None
    path = _root_dir() / f"{todo_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - corrupt file -> treat as missing
        return None


def delete_todo(todo_id: str) -> bool:
    path = _root_dir() / f"{todo_id}.json"
    if not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


@router.get("/{todo_id}")
def api_get_todo(todo_id: str) -> dict[str, Any]:
    todo = get_todo(todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="todo not found")
    return todo
