"""History persistence backed by MooFile (local BSON files, no browser storage).

The backend is the single source of truth for conversations. The SPA reads the
list on startup, fetches a full conversation when entering it, and submits
whole conversations at key lifecycle points (create / rename / edit / send /
stream-settle / delete) through per-conversation endpoints.

Storage layout: a single MooFile collection `conversations` under the root
directory; each record is one conversation object keyed by its `id` field.

Endpoints:
  GET    /api/history       -> conversation list, newest first (updatedAt desc)
  GET    /api/history/{id}  -> one full conversation (404 if missing)
  PUT    /api/history/{id}  -> upsert one conversation (create or replace)
  DELETE /api/history/{id}  -> remove one conversation

All writes are serialized with a process-wide lock so background writers (e.g.
the scheduled-task runner) can append to a conversation safely.
"""

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.moofile_util import MooFileUtil

router = APIRouter(prefix="/api/history", tags=["history"])

_HISTORY_DB = "conversations"
_INDEXES: list[str] = []  # small dataset; plain scan is fine
_LOCK = threading.Lock()


def _root_dir() -> Path:
    raw = os.getenv("HISTORY_ROOT", "./db/history").strip() or "./db/history"
    root = Path(raw)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent.parent / root
    return root


def _util() -> MooFileUtil:
    return MooFileUtil(str(_root_dir()))


def _ensure(util: MooFileUtil) -> None:
    if not util.database_exists(_HISTORY_DB):
        util.create_database(_HISTORY_DB)


@router.get("")
def list_history() -> list[dict[str, Any]]:
    """Return every stored conversation, newest first."""
    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_HISTORY_DB, {}, _INDEXES)
    rows.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
    return rows


@router.get("/{cid}")
def get_history(cid: str) -> dict[str, Any]:
    """Return one full conversation by id (404 when missing)."""
    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_HISTORY_DB, {"id": cid}, _INDEXES)
    if not rows:
        raise HTTPException(status_code=404, detail="conversation not found")
    return rows[0]


class HistoryPut(BaseModel):
    conversation: dict[str, Any]


@router.put("/{cid}")
def put_history(cid: str, payload: HistoryPut) -> dict[str, Any]:
    """Upsert one conversation: create it when missing, otherwise replace the
    whole record. The caller submits the full conversation object it holds in
    memory; no cross-record deletion happens here (that is DELETE's job)."""
    conv = dict(payload.conversation)
    conv["id"] = cid
    util = _util()
    _ensure(util)
    with _LOCK:
        existing = util.query_data(_HISTORY_DB, {"id": cid}, _INDEXES)
        if existing:
            util.update_data(_HISTORY_DB, {"id": cid}, conv, _INDEXES)
        else:
            util.insert_data(_HISTORY_DB, [conv], _INDEXES)
    return {"ok": True, "id": cid}


@router.delete("/{cid}")
def delete_history(cid: str) -> dict[str, Any]:
    """Remove a single conversation by id."""
    util = _util()
    _ensure(util)
    with _LOCK:
        util.delete_data(_HISTORY_DB, {"id": cid}, _INDEXES)
    return {"ok": True}
