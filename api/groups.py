"""Conversation folder management (groups under chat / task / timer categories).

The SPA stores conversation metadata in the MooFile history database
(`api/history.py`). This module adds user-defined groups (folders) on top of
the three built-in categories:

- chat  (普通会话 / chat-mode conversations, mode=chat)
- task  (工作任务 / task-mode conversations, mode=task; legacy sessions default to task)
- timer (定时任务 / timer sessions, id starts with "timer-")

A conversation references its group via the `groupId` field on its history
record. Deleting a group only clears `groupId` on its conversations — the
conversations themselves are never deleted here (that is DELETE /api/history).

Endpoints:
GET    /api/groups          -> all groups
POST   /api/groups          -> create {name, kind}
PUT    /api/groups/{id}     -> rename {name}
DELETE /api/groups/{id}     -> delete group; conversations' groupId -> null

Moving a conversation into / out of a group is done via the existing
PUT /api/history/{id} (the frontend submits the conversation with groupId).
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

from utils.moofile_util import MooFileUtil

router = APIRouter(prefix="/api/groups", tags=["groups"])

logger = logging.getLogger("api.groups")

_GROUPS_DB = "db"
_GROUPS_DIR_PREFIX = "db_groups_"
_INDEXES: list[str] = []  # small dataset; plain scan is fine
_LOCK = threading.Lock()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

KINDS = ("chat", "task", "timer", "all")


class GroupCreate(BaseModel):
    name: str
    kind: str


class GroupRename(BaseModel):
    name: str


def _root_dir() -> Path:
    """Resolve the groups database directory (same convention as history)."""
    raw = os.getenv("GROUPS_ROOT", "").strip()
    if raw:
        root = Path(raw)
        if not root.is_absolute():
            root = _PROJECT_ROOT / root
        return root

    db_dir = _PROJECT_ROOT / "db"
    if db_dir.is_dir():
        matches = sorted(p for p in db_dir.glob(f"{_GROUPS_DIR_PREFIX}*") if p.is_dir())
        if matches:
            return matches[0]

    root = db_dir / f"{_GROUPS_DIR_PREFIX}{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _util() -> MooFileUtil:
    return MooFileUtil(str(_root_dir()))


def _ensure(util: MooFileUtil) -> None:
    if not util.database_exists(_GROUPS_DB):
        util.create_database(_GROUPS_DB)
    _ensure_meta(_root_dir())


def _ensure_meta(root: Path) -> None:
    meta_file = root / "meta.json"
    if meta_file.is_file():
        return
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": root.name,
        "name": "groups",
        "type": "normal",
        "createdAt": now,
        "updatedAt": now,
        "trashed": False,
    }
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def list_groups() -> list[dict[str, Any]]:
    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_GROUPS_DB, {}, _INDEXES)
    groups = [dict(r) for r in rows]
    groups.sort(key=lambda g: (g.get("kind", ""), g.get("createdAt") or 0))
    return groups


def get_group(gid: str) -> dict[str, Any] | None:
    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_GROUPS_DB, {"id": gid}, _INDEXES)
    return dict(rows[0]) if rows else None


def create_group(name: str, kind: str) -> dict[str, Any]:
    kind = (kind or "").strip()
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {KINDS}")
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(name) > 30:
        raise HTTPException(status_code=400, detail="name too long (max 30 chars)")

    gid = f"g_{uuid.uuid4().hex[:8]}"
    group = {
        "id": gid,
        "name": name,
        "kind": kind,
        "createdAt": int(datetime.now().timestamp() * 1000),
    }
    util = _util()
    _ensure(util)
    with _LOCK:
        util.insert_data(_GROUPS_DB, [group], _INDEXES)
    return group


def rename_group(gid: str, name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(name) > 30:
        raise HTTPException(status_code=400, detail="name too long (max 30 chars)")
    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_GROUPS_DB, {"id": gid}, _INDEXES)
        if not rows:
            raise HTTPException(status_code=404, detail="group not found")
        merged = {**rows[0], "name": name}
        util.update_data(_GROUPS_DB, {"id": gid}, merged, _INDEXES)
    return merged


def delete_group(gid: str) -> dict[str, Any]:
    """Delete the group; its conversations' groupId is set to null (kept)."""
    from api.history import HistoryPut, list_history, put_history

    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_GROUPS_DB, {"id": gid}, _INDEXES)
        if not rows:
            raise HTTPException(status_code=404, detail="group not found")
        util.delete_data(_GROUPS_DB, {"id": gid}, _INDEXES)

    # 组内会话回到"未分组"：会话本身不删除。
    for conv in list_history():
        if conv.get("groupId") == gid:
            conv["groupId"] = None
            put_history(conv["id"], HistoryPut(conversation=conv))
    return {"ok": True, "id": gid}


@router.get("")
def api_list_groups() -> list[dict[str, Any]]:
    return list_groups()


@router.post("")
def api_create_group(body: GroupCreate) -> dict[str, Any]:
    return create_group(body.name, body.kind)


@router.put("/{gid}")
def api_rename_group(gid: str, body: GroupRename) -> dict[str, Any]:
    return rename_group(gid, body.name)


@router.delete("/{gid}")
def api_delete_group(gid: str) -> dict[str, Any]:
    return delete_group(gid)
