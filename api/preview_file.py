"""File preview API: read a local file by absolute path for clickable preview.

Endpoints:
  GET /api/preview-file?path=<absolute path> -> { name, path, content }

Security: absolute paths only, files only (no directories), 1MB cap,
utf-8 text read (errors replaced). Used by the frontend's clickable
absolute-path links rendered inside AI messages.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api")

MAX_PREVIEW_BYTES = 1024 * 1024  # 1MB cap


@router.get("/preview-file")
async def preview_file(path: str = Query(..., description="absolute file path")):
    try:
        p = Path(path)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid path")
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="must be an absolute path")
    if not p.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if p.is_dir():
        raise HTTPException(status_code=400, detail="cannot preview a directory")
    try:
        if p.stat().st_size > MAX_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail="file too large (max 1MB)")
        content = p.read_text(encoding="utf-8", errors="replace")
    except HTTPException:
        raise
    except OSError:
        raise HTTPException(status_code=500, detail="failed to read file")
    return {"name": p.name, "path": str(p), "content": content}
