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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from api.chat_voice.chat_voice import router as chat_voice_router
from api.attachments import router as attachments_router
from api.preview_file import router as preview_router
from api.reveal_file import router as reveal_router
from api.context import router as context_router
from api.history import router as history_router
from api.knowledge import router as knowledge_router
from api.manage.manager import router as manage_router
from api.timers import router as timers_router

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

def _should_open_browser() -> bool:
    """Whether the gateway should pop the default browser on startup.

    Controlled by the OPEN_BROWSER env var; enabled by default.
    """
    return os.getenv("OPEN_BROWSER", "1").strip().lower() in ("1", "true", "yes", "on")


async def _open_home_in_default_browser(url: str) -> None:
    """Background task: open the gateway page in the default browser.

    Uses the stdlib webbrowser module (a plain system command) instead of
    Playwright: a startup launcher does not need a heavy automation engine.
    Never blocks or fails startup.
    """
    import webbrowser

    try:
        await asyncio.sleep(1)  # let the HTTP server finish binding
        await asyncio.to_thread(webbrowser.open, url)
    except Exception:  # noqa: BLE001 - browser launcher is a convenience
        pass


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    """Startup: optionally open the gateway page in the default browser, and
    start the scheduled-task ticker (TIMER_ENABLED, default on).

    The browser opens in a background task so startup is never blocked; the
    timer scheduler runs until the app shuts down.
    """
    if _should_open_browser():
        host = "127.0.0.1"
        port = int(os.getenv("APP_PORT") or 8000)
        asyncio.create_task(_open_home_in_default_browser(f"http://{host}:{port}"))

    # Recover tasks that were left in `running` by a previous process (D10),
    # then start the scheduler loop (skipped when TIMER_ENABLED=false).
    from api import timers_engine

    try:
        timers_engine.recover_running()
    except Exception:  # noqa: BLE001 - recovery must never break startup
        pass
    timers_task = asyncio.create_task(timers_engine.scheduler_loop())
    try:
        yield
    finally:
        # Graceful shutdown: cancel the scheduler with a hard timeout so
        # Ctrl+C never hangs on an in-flight timer run (agent init / model
        # call may block past cancellation).
        timers_task.cancel()
        try:
            await asyncio.wait_for(timers_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            pass
        # Cancel any other lingering background tasks (browser launcher, etc.)
        current = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is current or task is timers_task:
                continue
            task.cancel()
        # Close MCP client subprocesses so the event loop can exit cleanly.
        try:
            from utils.mcp_loader import close_mcp_clients

            close_mcp_clients()
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass


app = FastAPI(title="ACP client base", lifespan=_app_lifespan)

# voice call sub-router: /api/chat_voice/voices, /api/chat_voice/chat
app.include_router(chat_voice_router)
# history persistence sub-router: /api/history (MooFile BSON files)
app.include_router(history_router)
# knowledge base gateway sub-router: /api/knowledge (external moofile_base)
app.include_router(knowledge_router)
# management panel sub-router: /api/manage/skills, /api/manage/tools, /api/manage/agents
app.include_router(manage_router)
# context compaction sub-router: /api/context/compact
app.include_router(context_router)
# attachment (image/file) sub-router: /api/attachments (MooFile-backed)
app.include_router(attachments_router)
app.include_router(preview_router)
app.include_router(reveal_router)
# scheduled tasks sub-router: /api/timers (MooFile-backed, cron/interval)
app.include_router(timers_router)

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
    # The frontend selects an agent by name; it is forwarded through the WS query
    # string and injected as AGENT_NAME so acp_agent.py assembles the right tools.
    agent_name = websocket.query_params.get("agent", "default") or "default"
    agent_env = os.environ.copy()
    agent_env["AGENT_NAME"] = agent_name
    proc = await asyncio.create_subprocess_exec(
        *AGENT_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(BASE_DIR),
        env=agent_env,
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
    """Serve frontend config (e.g. context window size, tokens)."""
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
    import socket

    import uvicorn

    HOST = os.getenv("APP_HOST") if os.getenv("APP_HOST") else "127.0.0.1"
    PORT = int(os.getenv("APP_PORT")) if os.getenv("APP_PORT") else 8000

    # Friendly guard: if the port is already taken, the gateway is likely
    # running already. Exit with a clear message instead of starting a second
    # instance that races for the port (browser opens, then shuts down).
    # Probe via loopback: works regardless of APP_HOST being 0.0.0.0 or 127.0.0.1.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"[gateway] 端口 {PORT} 已被占用，服务可能已在运行。")
            print("[gateway] 如需重启，请先停止旧实例（stop.ps1 或任务管理器结束对应 python 进程）。")
            sys.exit(1)

    uvicorn.run(
        "app:app",
        host=HOST,
        port=PORT,
        # Hard cap on graceful shutdown so Ctrl+C always exits even when a
        # websocket or background task does not finish promptly.
        timeout_graceful_shutdown=10,
    )