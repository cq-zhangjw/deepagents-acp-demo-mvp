"""Attachment API: upload / serve / list / delete (MooFile-backed).

Endpoints:
  POST   /api/attachments                  -> register one file (multipart)
  GET    /api/attachments/{file_id}        -> serve the blob (frontend <img> /
                                              future CDN switch point)
  GET    /api/attachments?session_id=...   -> list a session's attachments
  DELETE /api/attachments/{file_id}        -> delete one attachment
  DELETE /api/attachments?session_id=...   -> cascade-delete a session's
                                              attachments (records + blobs)

Authorization: GET /{file_id} optionally takes session_id to verify the
attachment belongs to that session (returns 403 otherwise). Session cascade is
also invoked from api/context.py when a session is deleted.
"""

import asyncio
import logging
import mimetypes
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import utils.attachment_store as store

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

logger = logging.getLogger("api.attachments")


def _public(record: dict) -> dict:
    """Public view of a stored record (no internal fields)."""
    return {
        "file_id": record.get("file_id"),
        "session_id": record.get("session_id"),
        "message_id": record.get("message_id"),
        "kind": record.get("kind"),
        "filename": record.get("filename"),
        "mime": record.get("mime"),
        "size": record.get("size"),
        "width": record.get("width"),
        "height": record.get("height"),
        "alt_text": record.get("alt_text"),
        "url": f"/api/attachments/{record.get('file_id')}",
        "created_at": record.get("created_at"),
    }


@router.post("")
async def upload_attachment(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    message_id: str = Form(None),
    kind: str = Form("image"),
    alt_text: str = Form(""),
):
    """Register one attachment: store the blob under _upload/ and insert its
    metadata record. Returns the logical file_id plus a serve URL."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if not session_id:
        raise HTTPException(status_code=400, detail="missing session_id")
    filename = os.path.basename(file.filename or "file")
    mime = (
        file.content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    try:
        record = await asyncio.to_thread(
            store.register,
            data,
            filename,
            session_id,
            message_id,
            kind,
            mime,
            alt_text,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("upload attachment failed session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"store failed: {e}") from e
    return {
        "ok": True,
        "file_id": record["file_id"],
        "url": f"/api/attachments/{record['file_id']}",
        "attachment": _public(record),
    }


@router.get("/{file_id}")
async def get_attachment(file_id: str, session_id: str | None = None):
    """Serve the attachment blob.

    ``session_id`` is an optional guard: when provided and it does not match the
    attachment's owner, the request is rejected (403) so attachments cannot be
    enumerated across sessions. This endpoint is the single entry point for the
    frontend ``<img>`` tag and the natural place to swap in a CDN later.
    """
    record = await asyncio.to_thread(store.get, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    if session_id and record.get("session_id") != session_id:
        raise HTTPException(status_code=403, detail="forbidden")
    path = await asyncio.to_thread(store.resolve_abs, record)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="blob missing")
    return FileResponse(
        path,
        media_type=record.get("mime") or "application/octet-stream",
        filename=record.get("filename") or None,
    )


@router.get("")
async def list_attachments(session_id: str):
    """List a session's attachments (oldest first)."""
    rows = await asyncio.to_thread(store.list_by_session, session_id)
    return {"ok": True, "attachments": [_public(r) for r in rows]}


@router.delete("/{file_id}")
async def delete_attachment(file_id: str):
    """Delete one attachment (record + blob). Idempotent."""
    removed = await asyncio.to_thread(store.delete, file_id)
    return {"ok": True, "removed": removed}


@router.delete("")
async def delete_session_attachments(session_id: str):
    """Cascade-delete a session's attachments (records + blobs)."""
    removed = await asyncio.to_thread(store.delete_by_session, session_id)
    return {"ok": True, "removed": removed}
