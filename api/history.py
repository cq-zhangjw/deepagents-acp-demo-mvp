"""History persistence backed by MooFile (local BSON files, no browser storage).

The backend is the single source of truth for conversations. The SPA reads the
list on startup, fetches a full conversation when entering it, and submits
whole conversations at key lifecycle points (create / rename / edit / send /
stream-settle / delete) through per-conversation endpoints.

Storage layout follows the moofile_base database convention so all data under
db/ is organized uniformly: one directory per database named
`db_history_<hash8>` (uuid4 hex prefix, same scheme as `db_<name>_<hash8>`
knowledge bases), containing a single MooFile collection `db.bson`. Each
record in the collection is one conversation object keyed by its `id` field.
`HISTORY_ROOT` can override the directory explicitly (default: auto-discover
`db/db_history_*`, create one on first run).

Endpoints:
  GET    /api/history       -> conversation list, newest first (updatedAt desc)
  GET    /api/history/{id}  -> one full conversation (404 if missing)
  PUT    /api/history/{id}  -> upsert one conversation (create or replace)
  DELETE /api/history/{id}  -> remove one conversation

All writes are serialized with a process-wide lock so background writers (e.g.
the scheduled-task runner) can append to a conversation safely.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.moofile_util import MooFileUtil

router = APIRouter(prefix="/api/history", tags=["history"])

# Collection file name inside the history database directory (matches the
# moofile_base layout where every database stores its BSON as db.bson).
_HISTORY_DB = "db"
_HISTORY_DIR_PREFIX = "db_history_"
_INDEXES: list[str] = []  # small dataset; plain scan is fine
_LOCK = threading.Lock()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _root_dir() -> Path:
    """Resolve the history database directory.

    Priority: HISTORY_ROOT env var (explicit override) -> first existing
    `db/db_history_*` directory -> a freshly created `db_history_<hash8>`.
    """
    raw = os.getenv("HISTORY_ROOT", "").strip()
    if raw:
        root = Path(raw)
        if not root.is_absolute():
            root = _PROJECT_ROOT / root
        return root

    db_dir = _PROJECT_ROOT / "db"
    if db_dir.is_dir():
        matches = sorted(p for p in db_dir.glob(f"{_HISTORY_DIR_PREFIX}*") if p.is_dir())
        if matches:
            return matches[0]

    # First run: create the database directory using the moofile_base naming
    # convention (db_<name>_<hash8>); later starts auto-discover it above.
    root = db_dir / f"{_HISTORY_DIR_PREFIX}{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _util() -> MooFileUtil:
    return MooFileUtil(str(_root_dir()))


def _ensure(util: MooFileUtil) -> None:
    if not util.database_exists(_HISTORY_DB):
        util.create_database(_HISTORY_DB)
    _ensure_meta(_root_dir())


def _ensure_meta(root: Path) -> None:
    """Write the database meta.json when missing (moofile_base layout).

    Every database directory under db/ carries a meta.json describing the
    collection; the history store is a normal (non-vector) database named
    "history" whose id equals its directory name.
    """
    meta_file = root / "meta.json"
    if meta_file.is_file():
        return
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": root.name,
        "name": "history",
        "type": "normal",
        "createdAt": now,
        "updatedAt": now,
        "trashed": False,
    }
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


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
    memory; no cross-record deletion happens here (that is DELETE's job).

    The record is tagged with moofile_base's data model fields (`recordType`,
    `dbId`) so the management UI - which browses records via
    `recordType == "record"` - can see and inspect chat history like any
    other plain table in the shared db/ root.
    """
    conv = dict(payload.conversation)
    conv["id"] = cid
    conv["recordType"] = "record"
    conv["dbId"] = _root_dir().name
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
