"""FastAPI bridge gateway: Web frontend <-> DeepAgents ACP Agent (stdio subprocess).

Responsibilities:
1. POST /upload          —— frontend uploads images/files, saves them, and returns an accessible HTTP resource URL
2. WS   /acp-ws          —— bidirectional forwarding between the frontend ACP client and the ACP agent subprocess
                           ACP JSON-RPC messages (including session/update, request_permission,
                           request_input, and other events)
3. /static, /uploads     —— static resource hosting (frontend page + uploaded files)

Usage: python gateway.py
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv
load_dotenv()


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ACP agent subprocess startup command
AGENT_CMD = [sys.executable, str(BASE_DIR / "acp_agent.py")]

app = FastAPI(title="DeepAgents ACP FastAPI Gateway")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Save the uploaded file and return an HTTP URL usable by ACP ResourceLink."""
    # Prevent path traversal: keep only the file name
    filename = Path(file.filename or "upload.bin").name
    save_path = UPLOAD_DIR / filename
    with save_path.open("wb") as f:
        f.write(await file.read())

    base = str(request.base_url).rstrip("/")
    return {
        "uri": f"{base}/uploads/{filename}",
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
    ws_reader = asyncio.create_task(forward_ws())
    # If either direction completes (usually the WS disconnects), terminate the subprocess to avoid agent leaks
    done, pending = await asyncio.wait(
        {ws_reader, forwarder}, return_when=asyncio.FIRST_COMPLETED
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


if __name__ == "__main__":
    import uvicorn
    HOST = os.getenv("APP_HOST") if os.getenv("APP_HOST") else "0.0.0.0"
    PORT = int(os.getenv("APP_PORT")) if os.getenv("APP_PORT") else 8000
    uvicorn.run("app:app", host=HOST, port=PORT)
