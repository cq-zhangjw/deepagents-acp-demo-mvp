"""Session context persistence backed by MooFile (independent of langgraph checkpoint).

Motivation
----------
The history actually fed to the AI agent used to live only inside the langgraph
checkpoint (`db/agent_state.sqlite`), which is written automatically and is hard
to control manually (editing / deleting / trimming / compacting all have to go
through RemoveMessage / aupdate_state internals).

This module makes the "real context given to the AI" a first-class MooFile
database (`db/db_session_content_<hash8>/db.bson` + `meta.json`, one record per
session), following the moofile_base storage convention used by the history
store (`db_history_*`), so all data under db/ is managed uniformly:

- The table is the **single source of truth** for "what the model sees next".
- The checkpoint remains the runtime store (agent internal state / breakpoints /
  crash recovery), but it is re-seeded from this table before every prompt and
  read back into it after every run (see `acp_agent.RollbackAgentServer.prompt`).
- The frontend display state (`db_history_*`) is untouched.

Storage format
--------------
One record per session keyed by `session_id`:

    {
        "session_id": "web-...",
        "messages": [<BaseMessage.to_json()>, ...],   # lossless round-trip
        "message_count": N,
        "updated_at": "ISO-8601"
    }

Messages are stored as `BaseMessage.to_json()` output so they can be restored
losslessly with `BaseMessage.from_json()` (same payload as the checkpoint).

Concurrency
-----------
Writers can come from several OS processes (the FastAPI gateway process and one
acp_agent subprocess per WebSocket). A process-wide `threading.Lock` plus an
exclusive advisory file lock (Windows `msvcrt.locking`) serialize every
read-modify-write on the BSON file. On non-Windows platforms the file lock
degrades to the in-process lock only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, convert_to_messages

from utils.moofile_util import MooFileUtil

# moofile_base storage convention: one directory per database under db/ named
# `db_<name>_<hash8>` (history uses `db_history_<hash8>`), containing a single
# MooFile collection `db.bson` plus a `meta.json` descriptor. The `db_` prefix
# is mandatory: moofile_base's list_db_dirs() only discovers `db_*` directories.
# The session context table follows the same layout so all data under db/ is
# managed uniformly and visible in the moofile_base UI.
DB_NAME = "db"                       # collection file inside the database dir
_DIR_PREFIX = "db_session_content_"  # directory prefix under db/
_META_NAME = "session_content"       # logical database name in meta.json

_INDEXES: list[str] = []  # small dataset; plain scan is fine

_LOCK = threading.Lock()  # in-process serialization

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Cross-process advisory lock file (sits inside the session-content database
# directory, alongside db.bson.lock, so db/ stays uniformly structured the
# same way as db_history_*; shared by every writer across gateway /
# acp_agent processes).
_LOCKFILE_NAME = ".session_content.lock"
_lockfile_handle: Any = None


def _db_parent_dir() -> Path:
    """Resolve the parent directory that holds session-content databases.

    Priority: DB_ROOT env var (absolute, or relative to the project root so the
    gateway process and acp_agent subprocesses resolve identically regardless of
    their cwd) -> project root / db.
    """
    raw = os.getenv("DB_ROOT", "").strip()
    if raw:
        p = Path(raw)
        if p.is_absolute():
            return p
        return _PROJECT_ROOT / p
    return _PROJECT_ROOT / "db"


def _root_dir() -> Path:
    """Resolve the session-content database directory.

    Priority: SESSION_CONTENT_ROOT env var (explicit override) -> first existing
    `db/db_session_content_*` directory -> a freshly created
    `db/db_session_content_<hash8>` (same discovery as api/history.py).
    """
    raw = os.getenv("SESSION_CONTENT_ROOT", "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        return p

    db_dir = _db_parent_dir()
    if db_dir.is_dir():
        matches = sorted(p for p in db_dir.glob(f"{_DIR_PREFIX}*") if p.is_dir())
        if matches:
            return matches[0]

    # First run: create the database directory using the moofile_base naming
    # convention; later starts auto-discover it above.
    root = db_dir / f"{_DIR_PREFIX}{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_meta(root: Path) -> None:
    """Write the database meta.json when missing (moofile_base layout).

    Identical descriptor shape to api/history.py's history store so db/
    contents stay uniformly manageable.
    """
    meta_file = root / "meta.json"
    if meta_file.is_file():
        return
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": root.name,
        "name": _META_NAME,
        "type": "normal",
        "createdAt": now,
        "updatedAt": now,
        "trashed": False,
    }
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _util() -> MooFileUtil:
    return MooFileUtil(str(_root_dir()))


def _ensure() -> None:
    """Create the database directory + collection + meta.json when missing."""
    root = _root_dir()
    util = MooFileUtil(str(root))
    if not util.database_exists(DB_NAME):
        util.create_database(DB_NAME)
    _ensure_meta(root)


def _lock_path() -> Path:
    return _root_dir() / _LOCKFILE_NAME


def _acquire_file_lock() -> None:
    """Acquire an exclusive advisory lock; blocks until available."""
    global _lockfile_handle
    root = _db_parent_dir()
    root.mkdir(parents=True, exist_ok=True)
    if _lockfile_handle is None:
        _lockfile_handle = open(_lock_path(), "a+", encoding="utf-8")  # noqa: SIM115 - kept open across calls
    handle = _lockfile_handle
    try:
        import msvcrt  # Windows only
    except ImportError:  # non-Windows: in-process lock only
        return
    handle.seek(0)
    if handle.read(1) == "":
        handle.write("\0")
        handle.flush()
    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(0.02)


def _release_file_lock() -> None:
    if _lockfile_handle is None:
        return
    try:
        import msvcrt
        _lockfile_handle.seek(0)
        msvcrt.locking(_lockfile_handle.fileno(), msvcrt.LK_UNLCK, 1)
    except (OSError, ImportError):
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json_list(messages: list[Any]) -> list[dict]:
    """Convert BaseMessage objects (or existing json dicts) to to_json payloads."""
    if not messages:
        return []
    if isinstance(messages[0], BaseMessage):
        return [m.to_json() for m in messages]
    return list(messages)


def from_json_list(payload: list[dict]) -> list[BaseMessage]:
    """Restore BaseMessage objects from stored to_json payloads."""
    return convert_to_messages(payload)


def get_context(session_id: str) -> list[dict] | None:
    """Return the session's context messages (to_json dict list), or None if the
    table has no record for this session yet (caller decides whether to seed it
    from the checkpoint)."""
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            rows = _util().query_data(DB_NAME, {"session_id": session_id}, _INDEXES)
            if not rows:
                return None
            return list(rows[0].get("messages") or [])
        finally:
            _release_file_lock()


def save_context(session_id: str, messages: list[Any]) -> None:
    """Overwrite the session's message list (upsert by session_id).

    `messages` may be BaseMessage objects or to_json dicts.
    """
    payload = _to_json_list(messages)
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            util = _util()
            existing = util.query_data(DB_NAME, {"session_id": session_id}, _INDEXES)
            record = {
                # `recordType` follows moofile_base's data model: its record
                # list API filters on {"recordType": "record"}, so without this
                # field the table shows up in the 8888 UI with zero records.
                "recordType": "record",
                "session_id": session_id,
                "messages": payload,
                "message_count": len(payload),
                "created_at": _now(),
                "updated_at": _now(),
            }
            if existing:
                util.update_data(
                    DB_NAME,
                    {"session_id": session_id},
                    {
                        "messages": payload,
                        "message_count": len(payload),
                        "updated_at": _now(),
                    },
                    _INDEXES,
                )
            else:
                util.insert_data(DB_NAME, [record], _INDEXES)
        finally:
            _release_file_lock()


def init_from_checkpoint(session_id: str, checkpoint_messages: list[Any]) -> list[dict]:
    """First-run seeding: copy checkpoint BaseMessages into the table and return
    the stored to_json list. The table is only seeded when it has no record."""
    payload = _to_json_list(checkpoint_messages)
    save_context(session_id, payload)
    return payload


def update_message(session_id: str, message_json: dict) -> bool:
    """Replace one message by id (manual editing, phase-2 API).

    The session's sqlite checkpoint mirror is invalidated afterwards so no
    stale copy survives; the next prompt rebuilds it from the table.
    Attachments whose ``[图片: id]`` placeholders disappeared in the edit are
    cascade-deleted from the attachment store.
    """
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            util = _util()
            rows = util.query_data(DB_NAME, {"session_id": session_id}, _INDEXES)
            if not rows:
                return False
            messages = list(rows[0].get("messages") or [])
            target_id = _msg_id(message_json)
            for i, m in enumerate(messages):
                if _msg_id(m) == target_id:
                    _cascade_removed_images(session_id, m, message_json)
                    messages[i] = message_json
                    util.update_data(
                        DB_NAME,
                        {"session_id": session_id},
                        {
                            "messages": messages,
                            "message_count": len(messages),
                            "updated_at": _now(),
                        },
                        _INDEXES,
                    )
                    _delete_checkpoint_thread(session_id)
                    return True
            return False
        finally:
            _release_file_lock()


def delete_message(session_id: str, message_id: str) -> bool:
    """Remove one message by id (phase-2 API).

    The session's sqlite checkpoint mirror is invalidated afterwards so no
    stale copy survives; the next prompt rebuilds it from the table.
    Attachments bound to the removed message are cascade-deleted from the
    attachment store.
    """
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            util = _util()
            rows = util.query_data(DB_NAME, {"session_id": session_id}, _INDEXES)
            if not rows:
                return False
            messages = list(rows[0].get("messages") or [])
            filtered = [m for m in messages if _msg_id(m) != message_id]
            if len(filtered) == len(messages):
                return False
            util.update_data(
                DB_NAME,
                {"session_id": session_id},
                {
                    "messages": filtered,
                    "message_count": len(filtered),
                    "updated_at": _now(),
                },
                _INDEXES,
            )
            _delete_checkpoint_thread(session_id)
            _cascade_message_attachments(session_id, message_id)
            return True
        finally:
            _release_file_lock()


def delete_session(session_id: str) -> bool:
    """Delete a session everywhere it is persisted.

    Removes the record from the context table, deletes the session's
    langgraph checkpoint thread from agent_state.sqlite, and cascade-deletes
    the session's attachments (records + blobs) from the attachment store, so
    no stale state survives a session deletion. Idempotent: returns True if
    anything was actually removed.
    """
    removed_any = False
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            util = _util()
            rows = util.query_data(DB_NAME, {"session_id": session_id}, _INDEXES)
            if rows:
                util.delete_data(DB_NAME, {"session_id": session_id}, _INDEXES)
                removed_any = True
        finally:
            _release_file_lock()
    # Delete the langgraph checkpoint thread. sqlite is written by the gateway
    # and the agent subprocesses, so it lives outside the bson file lock.
    if _delete_checkpoint_thread(session_id):
        removed_any = True
    # Cascade-delete the session's attachments (records + blobs).
    if _cascade_session_attachments(session_id):
        removed_any = True
    return removed_any


def _checkpoint_db_path() -> Path:
    """Path of the langgraph checkpoint database (agent_state.sqlite)."""
    return _db_parent_dir() / "agent_state.sqlite"


def _delete_checkpoint_thread(session_id: str) -> bool:
    """Drop checkpoints + writes for one thread id (langgraph sqlite schema).

    Mirrors AsyncSqliteSaver.adelete_thread but is callable from the HTTP
    gateway process, which cannot reach the agent subprocess's checkpointer.
    """
    db_path = _checkpoint_db_path()
    if not db_path.is_file():
        return False
    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM checkpoints WHERE thread_id = ?", (str(session_id),))
            cur.execute("DELETE FROM writes WHERE thread_id = ?", (str(session_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def rollback_last_turn(session_id: str) -> bool:
    """Drop the last user turn (and everything after it) from the table.

    Used by retry: the client re-sends the same user text, so keeping the
    original user message would duplicate it.
    """
    msgs = get_context(session_id)
    if msgs is None:
        return False
    objs = from_json_list(msgs)
    last_human_idx = None
    for i in range(len(objs) - 1, -1, -1):
        if isinstance(objs[i], HumanMessage) or getattr(objs[i], "type", "") == "human":
            last_human_idx = i
            break
    if last_human_idx is None:
        return False
    save_context(session_id, objs[:last_human_idx])
    return True


def _msg_id(message: dict) -> str | None:
    """Extract the message id from a to_json payload (lives under kwargs.id)."""
    kwargs = message.get("kwargs") if isinstance(message, dict) else {}
    return kwargs.get("id") if isinstance(kwargs, dict) else None


# --- Attachment cascade helpers ---------------------------------------------
# Attachments (images / files) are persisted in the separate MooFile store
# utils/attachment_store (db/db_attachments_*). Deletion of a session or a
# message must also clear the associated attachments so no orphaned blob or
# metadata record survives. Imports are kept function-local so this module
# stays side-effect free at import time (it is loaded by the acp_agent
# subprocess too).

_IMAGE_PLACEHOLDER_RE = re.compile(r"\[图片:\s*([a-zA-Z0-9_-]+)\]")


def _message_text(message: dict) -> str:
    """Best-effort extraction of a message's plain-text content."""
    kwargs = message.get("kwargs") if isinstance(message, dict) else {}
    if isinstance(kwargs, dict):
        content = kwargs.get("content")
        if isinstance(content, str):
            return content
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


def _extract_image_ids(content: str) -> set[str]:
    return set(_IMAGE_PLACEHOLDER_RE.findall(content or ""))


def _cascade_session_attachments(session_id: str) -> bool:
    """Delete all attachments of a session; returns True if any was removed."""
    try:
        from utils.attachment_store import delete_by_session
        return delete_by_session(session_id) > 0
    except Exception:  # noqa: BLE001 - cascade must never break the deletion
        return False


def _cascade_message_attachments(session_id: str, message_id: str) -> bool:
    """Delete attachments bound to one message; returns True if any removed."""
    try:
        from utils.attachment_store import delete_by_message
        return delete_by_message(session_id, message_id) > 0
    except Exception:  # noqa: BLE001 - cascade must never break the deletion
        return False


def _cascade_removed_images(session_id: str, old_message: dict, new_message: dict) -> None:
    """Delete attachments whose ``[图片: id]`` placeholders disappeared in an edit."""
    try:
        old_ids = _extract_image_ids(_message_text(old_message))
        new_ids = _extract_image_ids(_message_text(new_message))
        removed = old_ids - new_ids
        if not removed:
            return
        from utils.attachment_store import delete
        for fid in removed:
            delete(fid)
    except Exception:  # noqa: BLE001 - cascade must never break the edit
        pass
