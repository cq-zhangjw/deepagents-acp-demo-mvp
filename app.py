"""FastAPI bridge gateway: Web frontend <-> DeepAgents ACP Agent (stdio subprocess).

Responsibilities:
1. POST /upload          —— frontend uploads images/files, saves them, and returns an accessible HTTP resource URL
2. WS   /acp-ws          —— bidirectional forwarding between the frontend ACP client and the ACP agent subprocess
                           ACP JSON-RPC messages (including session/update, request_permission,
                           request_input, and other events)
3. /, /uploads           —— built Vue client and uploaded-file hosting

Usage: python gateway.py
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv
load_dotenv()


BASE_DIR = Path(__file__).resolve().parent
# Keep static/ for the legacy demo assets; the active SPA is built into web/dist.
WEB_DIST_DIR = BASE_DIR / "web" / "dist"
UPLOAD_DIR = Path(os.getenv("UPLOAD_ROOT", "uploads").strip() or "uploads")
if not UPLOAD_DIR.is_absolute():
    UPLOAD_DIR = BASE_DIR / UPLOAD_DIR
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ACP agent subprocess startup command
AGENT_CMD = [sys.executable, str(BASE_DIR / "acp_agent.py")]

app = FastAPI(title="ACP client base")

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Save an upload and return its project-relative path for local Agent tools."""
    # Prevent path traversal: keep only the file name
    filename = Path(file.filename or "upload.bin").name
    save_path = UPLOAD_DIR / filename
    with save_path.open("wb") as f:
        f.write(await file.read())

    return {
        "path": f"/uploads/{filename}",
        "name": filename,
        "mimeType": file.content_type,
    }


@app.websocket("/acp-ws")
async def acp_bridge(websocket: WebSocket):
    """Each WebSocket connection corresponds to an independent ACP agent subprocess, forwarding messages bidirectionally."""
    await websocket.accept()
    proc = await asyncio.create_subprocess_exec(
        *AGENT_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(BASE_DIR),
        # ACP JSON-RPC messages can include Base64 image blocks and exceed asyncio's 64 KiB default.
        limit=16 * 1024 * 1024,
    )

    async def forward_stdout():
        """Subprocess stdout (ACP events) -> frontend WebSocket"""
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8").strip()
            if text:
                try:
                    await websocket.send_text(text)
                except Exception:
                    break

    async def forward_stderr():
        """Keep agent diagnostics visible without mixing them into ACP stdout."""
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            print(f"[acp-agent] {line.decode('utf-8', errors='replace').rstrip()}")

    async def forward_ws():
        """Frontend WebSocket (ACP requests) -> subprocess stdin"""
        try:
            while True:
                msg = await websocket.receive_text()
                proc.stdin.write((msg + "\n").encode("utf-8"))
                await proc.stdin.drain()
        except WebSocketDisconnect:
            pass

    forwarder = asyncio.create_task(forward_stdout())
    error_forwarder = asyncio.create_task(forward_stderr())
    ws_reader = asyncio.create_task(forward_ws())
    # If either direction completes (usually the WS disconnects), terminate the subprocess to avoid agent leaks
    done, pending = await asyncio.wait(
        {ws_reader, forwarder, error_forwarder}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await _terminate_process_tree(proc)


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate the subprocess and its entire process tree.

    On Windows, the venv python.exe is a launcher + real process pair.
    Only terminating the launcher may leave the busy real process behind as an orphan, so the whole tree must be killed.
    """
    if sys.platform == "win32":
        try:
            # taskkill /T recursively kills child processes, /F forces them
            await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Give taskkill a moment to finish
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return
        except Exception:
            pass  # Fall back to regular terminate
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()


@app.get("/api/config")
async def get_config():
    """下发前端所需配置（如上下文窗口大小，tokens）。"""
    content_size = os.getenv("CONTENT_SIZE", "").strip()
    try:
        content_size = int(content_size) if content_size else 0
    except ValueError:
        content_size = 0
    return {"contentSize": content_size}


# Mount the SPA after API and WebSocket routes so it only handles client asset requests.
app.mount("/", StaticFiles(directory=str(WEB_DIST_DIR), html=True), name="web-client")


@app.middleware("http")
async def no_cache_spa_index(request, call_next):
    """Prevent the SPA entry (index.html) from being cached so updates always take effect on refresh."""
    response = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    import uvicorn
    HOST = os.getenv("APP_HOST") if os.getenv("APP_HOST") else "0.0.0.0"
    PORT = int(os.getenv("APP_PORT")) if os.getenv("APP_PORT") else 8000
    uvicorn.run("app:app", host=HOST, port=PORT)
