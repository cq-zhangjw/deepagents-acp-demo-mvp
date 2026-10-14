"""Attachment (image / file) store backed by MooFile.

Follows the moofile_base storage convention used by the history store
(``db_history_*``) and the session-context table (``db_session_content_*``):

    db/db_attachments_<hash8>/
        db.bson          # one metadata record per attachment
        db.bson.meta     # MooFile internal metadata (generated on demand)
        meta.json        # {"name": "attachments", ...}
        _upload/         # blob files, named {file_id}{ext}

Design decisions
----------------
- **Metadata in MooFile, blobs in _upload/** (the same "index + blob" split
  object stores use). Blobs are never embedded in the BSON collection.
- **References are logical file_ids**, never absolute paths. Moving the whole
  ``db_attachments_*`` directory keeps every reference valid.
- Every attachment carries ``session_id`` (cascade deletion + access check)
  and optionally ``message_id`` (per-message cascade).
- ``recordType: "record"`` so the collection shows up in the moofile_base UI.

Concurrency
-----------
Writers can come from several OS processes (the FastAPI gateway process, the
acp_agent subprocesses, and MCP servers such as browser_server). A process-wide
``threading.Lock`` plus an exclusive advisory file lock (Windows
``msvcrt.locking``) serialize every read-modify-write on the BSON file. On
non-Windows platforms the file lock degrades to the in-process lock only.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.moofile_util import MooFileUtil

# Collection file inside the database directory (matches the moofile_base
# layout where every database stores its BSON as db.bson).
DB_NAME = "db"
_DIR_PREFIX = "db_attachments_"  # directory prefix under db/
_META_NAME = "attachments"       # logical database name in meta.json
_UPLOAD_DIR_NAME = "_upload"     # blob directory inside the database dir

_INDEXES: list[str] = []  # small dataset; plain scan is fine

_LOCK = threading.Lock()  # in-process serialization

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Cross-process advisory lock file (sits inside the attachment database
# directory, shared by every writer across gateway / acp_agent / MCP
# processes).
_LOCKFILE_NAME = ".attachments.lock"
_lockfile_handle: Any = None


def _db_parent_dir() -> Path:
    """Resolve the parent directory that holds attachment databases.

    Priority: DB_ROOT env var (absolute, or relative to the project root so
    every process resolves identically regardless of its cwd) -> project
    root / db.
    """
    raw = os.getenv("DB_ROOT", "").strip()
    if raw:
        p = Path(raw)
        if p.is_absolute():
            return p
        return _PROJECT_ROOT / p
    return _PROJECT_ROOT / "db"


def _root_dir() -> Path:
    """Resolve the attachment database directory.

    Priority: ATTACHMENT_ROOT env var (explicit override) -> first existing
    `db/db_attachments_*` directory -> a freshly created
    `db/db_attachments_<hash8>` (same discovery as api/history.py).
    """
    raw = os.getenv("ATTACHMENT_ROOT", "").strip()
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
    """Write the database meta.json when missing (moofile_base layout)."""
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


def register(
    data: bytes,
    filename: str,
    session_id: str,
    message_id: str | None = None,
    kind: str = "image",
    mime: str = "application/octet-stream",
    alt_text: str = "",
    width: int = 0,
    height: int = 0,
) -> dict:
    """Save one attachment blob under _upload/ and insert its metadata record.

    Args:
        data: Raw bytes of the attachment.
        filename: Display name (original name / generated name).
        session_id: Owning session (cascade deletion + access checks).
        message_id: Optional owning message within the session.
        kind: ``image`` / ``audio`` / ``file``.
        mime: MIME type used when serving the blob.
        alt_text: One-line description used as a cheap LLM-context placeholder.
        width / height: Optional pixel dimensions (images).

    Returns:
        The stored metadata record (including ``file_id``).

    Raises:
        Any storage error; a partially written blob is rolled back.
    """
    file_id = uuid.uuid4().hex
    ext = Path(filename).suffix or ""
    storage_name = f"{file_id}{ext}"
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            root = _root_dir()
            upload_dir = root / _UPLOAD_DIR_NAME
            upload_dir.mkdir(parents=True, exist_ok=True)
            (upload_dir / storage_name).write_bytes(data)
            record = {
                "recordType": "record",
                "file_id": file_id,
                "session_id": session_id,
                "message_id": message_id,
                "kind": kind,
                "filename": filename,
                "storage_path": f"{_UPLOAD_DIR_NAME}/{storage_name}",
                "mime": mime,
                "size": len(data),
                "width": width,
                "height": height,
                "alt_text": alt_text,
                "created_at": _now(),
                "updated_at": _now(),
            }
            _util().insert_data(DB_NAME, [record], _INDEXES)
            return record
        except Exception:
            # Roll back the partially written blob so no orphan file remains.
            upload_dir = _root_dir() / _UPLOAD_DIR_NAME
            f = upload_dir / storage_name
            if f.is_file():
                try:
                    f.unlink()
                except OSError:  # noqa: BLE001 - best effort rollback
                    pass
            raise
        finally:
            _release_file_lock()


def get(file_id: str) -> dict | None:
    """Return the metadata record for one file_id, or None."""
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            rows = _util().query_data(DB_NAME, {"file_id": file_id}, _INDEXES)
            return rows[0] if rows else None
        finally:
            _release_file_lock()


def resolve_abs(record: dict) -> Path:
    """Absolute filesystem path of an attachment's blob (relative to the db dir)."""
    return _root_dir() / record.get("storage_path", "")


def list_by_session(session_id: str) -> list[dict]:
    """Return all attachments of a session, oldest first."""
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            rows = _util().query_data(DB_NAME, {"session_id": session_id}, _INDEXES)
            rows.sort(key=lambda r: r.get("created_at") or "")
            return rows
        finally:
            _release_file_lock()


def list_by_message(session_id: str, message_id: str) -> list[dict]:
    """Return all attachments bound to a specific message of a session."""
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            rows = _util().query_data(
                DB_NAME, {"session_id": session_id, "message_id": message_id}, _INDEXES
            )
            rows.sort(key=lambda r: r.get("created_at") or "")
            return rows
        finally:
            _release_file_lock()


def _unlink_blobs(records: list[dict]) -> None:
    """Best-effort removal of blob files referenced by the given records."""
    root = _root_dir()
    for row in records:
        p = root / row.get("storage_path", "")
        if p.is_file():
            try:
                p.unlink()
            except OSError:  # noqa: BLE001 - best effort cleanup
                pass


def delete(file_id: str) -> bool:
    """Delete one attachment (record + blob). Idempotent.

    Returns:
        True if a record was actually removed.
    """
    removed = False
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            util = _util()
            rows = util.query_data(DB_NAME, {"file_id": file_id}, _INDEXES)
            if rows:
                util.delete_data(DB_NAME, {"file_id": file_id}, _INDEXES)
                removed = True
                _unlink_blobs(rows)
        finally:
            _release_file_lock()
    return removed


def delete_by_session(session_id: str) -> int:
    """Delete all attachments of a session (records + blobs).

    Returns:
        Number of attachments removed.
    """
    removed = 0
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            util = _util()
            rows = util.query_data(DB_NAME, {"session_id": session_id}, _INDEXES)
            if rows:
                util.delete_data(DB_NAME, {"session_id": session_id}, _INDEXES)
                removed = len(rows)
                _unlink_blobs(rows)
        finally:
            _release_file_lock()
    return removed


def delete_by_message(session_id: str, message_id: str) -> int:
    """Delete all attachments bound to one message (records + blobs).

    Returns:
        Number of attachments removed.
    """
    removed = 0
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            util = _util()
            rows = util.query_data(
                DB_NAME, {"session_id": session_id, "message_id": message_id}, _INDEXES
            )
            if rows:
                util.delete_data(
                    DB_NAME,
                    {"session_id": session_id, "message_id": message_id},
                    _INDEXES,
                )
                removed = len(rows)
                _unlink_blobs(rows)
        finally:
            _release_file_lock()
    return removed


def count() -> int:
    """Total number of attachment records (for stats / tests)."""
    with _LOCK:
        _acquire_file_lock()
        try:
            _ensure()
            return _util().count_data(DB_NAME, {}, _INDEXES)
        finally:
            _release_file_lock()
