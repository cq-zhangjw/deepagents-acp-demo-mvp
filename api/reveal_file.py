"""Reveal-in-file-manager API: open an absolute path in the OS file manager.

Endpoints:
  GET /api/reveal-file?path=<absolute path> -> { ok, kind, path }

Security: absolute paths only. Directories are opened directly in Explorer;
files are opened with the parent directory shown and the file selected
(explorer /select). Used by the frontend's clickable absolute-path links,
replacing the previous in-app preview modal.
"""
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api")


@router.get("/reveal-file")
async def reveal_file(path: str = Query(..., description="absolute path")):
    try:
        p = Path(path)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid path")
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="must be an absolute path")
    if not p.exists():
        raise HTTPException(status_code=404, detail="path not found")

    if p.is_dir():
        _open_in_manager(p, select=False)
        return {"ok": True, "kind": "dir", "path": str(p)}
    if p.is_file():
        _open_in_manager(p, select=True)
        return {"ok": True, "kind": "file", "path": str(p)}
    raise HTTPException(status_code=400, detail="not a file or directory")


def _open_in_manager(p: Path, select: bool) -> None:
    """Open in the OS file manager without blocking the request."""
    try:
        if os.name == "nt":
            # Windows: open dir directly, or open dir with file selected.
            # /select,<path> must stay a single argument so spaces are safe.
            if select:
                subprocess.Popen(["explorer.exe", f"/select,{p}"])
            else:
                subprocess.Popen(["explorer.exe", str(p)])
        else:
            # POSIX fallback: open containing directory.
            subprocess.Popen(["xdg-open", str(p.parent if select else p)])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to open file manager: {e}")
