"""History persistence backed by MooFile (local BSON files, no browser storage).

Replaces the frontend localStorage conversation list: the SPA now reads and
writes the whole conversation set through these endpoints, while MooFile keeps
the data on disk under db/history (HISTORY_ROOT env, default ./db/history),
fully local and portable across machines.

Storage layout: a single MooFile collection `conversations` under the root
directory; each record is one conversation object keyed by its `id` field.

Endpoints:
  GET    /api/history   -> all conversations, newest first (updatedAt desc)
  PUT    /api/history   -> full-sync replace of the conversation set
  DELETE /api/history/{id} -> remove one conversation
"""

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from utils.moofile_util import MooFileUtil

router = APIRouter(prefix="/api/history", tags=["history"])

_HISTORY_DB = "conversations"
_INDEXES: list[str] = []  # small dataset; plain scan is fine


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
    rows = util.query_data(_HISTORY_DB, {}, _INDEXES)
    rows.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
    return rows


class HistoryPut(BaseModel):
    conversations: list[dict[str, Any]]


@router.put("")
def put_history(payload: HistoryPut) -> dict[str, Any]:
    """Full-sync: upsert the provided conversations and drop any record that
    is no longer in the list (mirrors the old localStorage replace semantics)."""
    util = _util()
    _ensure(util)
    incoming = payload.conversations
    incoming_ids = {c.get("id") for c in incoming if c.get("id")}

    for conv in incoming:
        cid = conv.get("id")
        if not cid:
            continue
        existing = util.query_data(_HISTORY_DB, {"id": cid}, _INDEXES)
        if existing:
            util.update_data(_HISTORY_DB, {"id": cid}, conv, _INDEXES)
        else:
            util.insert_data(_HISTORY_DB, [conv], _INDEXES)

    # remove records that disappeared from the frontend list
    for row in util.query_data(_HISTORY_DB, {}, _INDEXES):
        rid = row.get("id")
        if rid and rid not in incoming_ids:
            util.delete_data(_HISTORY_DB, {"id": rid}, _INDEXES)

    return {"ok": True, "count": len(incoming)}


@router.delete("/{cid}")
def delete_history(cid: str) -> dict[str, Any]:
    """Remove a single conversation by id."""
    util = _util()
    _ensure(util)
    util.delete_data(_HISTORY_DB, {"id": cid}, _INDEXES)
    return {"ok": True}
