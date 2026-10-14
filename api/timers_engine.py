"""定时任务引擎：MooFile 存储 + 调度器 + 进程内 ACP 执行（无人值守）。

存储对齐 `api/history.py` 的模式：
  - 目录 `db/db_timer_tasks_<hash8>/`（moofile_base `db_<name>_<hash8>` 命名规范，
    知识库管理 UI 可见），`TIMER_TASKS_ROOT` 可显式覆盖目录；
  - `db.bson` collection，每条记录 = 一个任务；
  - 进程级 `threading.Lock` 串行写。

执行引擎复用 `acp_agent.RollbackAgentServer`（与手动对话同一条
build_agent 装配链、同一 sqlite checkpointer / session_context 表），
通过 TimerConn 替身把流式更新丢弃、把 `request_permission` 自动拒绝
（无人值守策略，D8），任务执行串行（单 server 实例 + 任务级互斥，D9）。

调度器：FastAPI lifespan 启动 asyncio 后台任务，每 `TIMER_TICK_SECONDS`
tick 一次；cron（croniter）与 intervalMinutes 二选一；重启时把遗留
`running` 记录补 `cancelled`（D10）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from croniter import croniter

from utils.moofile_util import MooFileUtil

logger = logging.getLogger("api.timers_engine")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---- 存储（对齐 api/history.py） ----
_TIMER_DIR_PREFIX = "db_timer_tasks_"
_TIMER_DB = "db"  # collection file name inside the timer database directory
_INDEXES: list[str] = []  # small dataset; plain scan is fine
_LOCK = threading.Lock()


def _root_dir() -> Path:
    """Timer tasks database directory.

    `TIMER_TASKS_ROOT` can override explicitly; otherwise auto-discover
    `db/db_timer_tasks_*` (newest by mtime), creating one on first run.
    """
    override = os.getenv("TIMER_TASKS_ROOT", "").strip()
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p
    db_dir = _PROJECT_ROOT / "db"
    if db_dir.is_dir():
        matches = sorted(p for p in db_dir.glob(f"{_TIMER_DIR_PREFIX}*") if p.is_dir())
        if matches:
            return matches[-1]
    root = db_dir / f"{_TIMER_DIR_PREFIX}{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _util() -> MooFileUtil:
    return MooFileUtil(str(_root_dir()))


def _ensure(util: MooFileUtil) -> None:
    if not util.database_exists(_TIMER_DB):
        util.create_database(_TIMER_DB)
    _ensure_meta(_root_dir())


def _ensure_meta(root: Path) -> None:
    """Write the database meta.json when missing (moofile_base layout)."""
    meta_file = root / "meta.json"
    if meta_file.exists():
        return
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": root.name,
        "name": "timer_tasks",
        "description": "定时任务（Schedule & Task）",
        "type": "normal",
        "createdAt": now,
        "updatedAt": now,
        "trashed": False,
    }
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


# ---- 任务 CRUD（数据层） ----
def list_tasks(enabled_only: bool = False) -> list[dict[str, Any]]:
    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_TIMER_DB, {"enabled": True} if enabled_only else {}, _INDEXES)
    rows.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
    return rows


def get_task(task_id: str) -> dict[str, Any] | None:
    util = _util()
    _ensure(util)
    with _LOCK:
        rows = util.query_data(_TIMER_DB, {"id": task_id}, _INDEXES)
    return rows[0] if rows else None


def save_task(task: dict[str, Any]) -> None:
    util = _util()
    _ensure(util)
    with _LOCK:
        existing = util.query_data(_TIMER_DB, {"id": task["id"]}, _INDEXES)
        if existing:
            util.update_data(_TIMER_DB, {"id": task["id"]}, task, _INDEXES)
        else:
            util.insert_data(_TIMER_DB, [task], _INDEXES)


def delete_task(task_id: str) -> None:
    util = _util()
    _ensure(util)
    with _LOCK:
        util.delete_data(_TIMER_DB, {"id": task_id}, _INDEXES)


# ---- 无人值守 ACP 连接替身 ----
class TimerConn:
    """ACP 连接替身：流式 session_update 丢弃；权限请求自动拒绝（D8）。

    定时任务无人值守：执行期间产生 request_permission / request_input
    一律视为拒绝，并在 `permission_denied` 上留痕，执行结束后任务记 failed。
    """

    def __init__(self) -> None:
        self.permission_denied = False

    async def session_update(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None

    async def request_permission(self, **kwargs: Any) -> Any:  # noqa: ARG002
        self.permission_denied = True
        from acp.schema import RequestPermissionResponse, SelectedPermissionOutcome

        return RequestPermissionResponse(
            outcome=SelectedPermissionOutcome(outcome="selected", option_id="reject")
        )


# ---- 单例执行 server ----
_server: Any = None
_server_conn: TimerConn | None = None
_server_lock = asyncio.Lock()


async def _get_server() -> tuple[Any, TimerConn]:
    """Lazy-create the in-process RollbackAgentServer (one sqlite checkpointer)."""
    global _server, _server_conn
    if _server is not None:
        return _server, _server_conn
    async with _server_lock:
        if _server is not None:
            return _server, _server_conn
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from acp_agent import RollbackAgentServer, build_agent

        db_root = os.getenv("DB_ROOT", "").strip() or "./db"
        if not os.path.isabs(db_root):
            db_root = str(_PROJECT_ROOT / db_root)
        os.makedirs(db_root, exist_ok=True)
        db_path = os.path.join(db_root, "agent_state.sqlite")
        conn = await aiosqlite.connect(db_path)
        checkpointer = AsyncSqliteSaver(conn)
        _server = RollbackAgentServer(
            agent=lambda ctx, agent_name=None: build_agent(ctx, checkpointer, agent_name=agent_name),
            load_sessions=True,
        )
        _server_conn = TimerConn()
        _server.on_connect(_server_conn)
        logger.info("[timers] in-process RollbackAgentServer ready (checkpoint=%s)", db_path)
    return _server, _server_conn


# ---- 会话履历写入（后端直接写，对齐前端 Conversation 结构） ----
def _make_user_msg(text: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "role": "user",
        "text": text,
        "attachments": [],
        "createdAt": _now_ms(),
    }


def _schedule_text(task: dict[str, Any]) -> str:
    """任务的周期信息（用于会话履历展示）。"""
    if task.get("schedule"):
        return f"Cron `{task['schedule']}`"
    if task.get("intervalMinutes"):
        return f"每 {task['intervalMinutes']} 分钟"
    return "—"


def _make_timer_user_msg(task: dict[str, Any], trigger_text: str) -> dict[str, Any]:
    """定时任务的「用户消息」：结构化展示任务类型 / 周期 / 触发方式 / Prompt。"""
    name = (task.get("name") or "定时任务").strip()
    prompt = (task.get("prompt") or "").strip()
    task_type = "Cron 定时" if task.get("schedule") else "间隔执行"
    text = (
        f"### ⏰ 定时任务执行\n"
        f"- **任务名称**：{name}\n"
        f"- **任务类型**：{task_type}\n"
        f"- **周期**：{_schedule_text(task)}\n"
        f"- **触发方式**：{trigger_text}\n"
        f"- **Prompt**：{prompt}"
    )
    return _make_user_msg(text)


def _make_assistant_msg(
    final_text: str, status: str, started_at: int, msg_id: str | None = None
) -> dict[str, Any]:
    completed_at = _now_ms()
    return {
        "id": msg_id or uuid.uuid4().hex,
        "role": "assistant",
        "finalText": final_text,
        "status": status,
        "process": {
            "startedAt": started_at,
            "completedAt": completed_at,
            "plan": [],
            "analyses": [],
            "toolCalls": [],
        },
        "segments": [{"id": uuid.uuid4().hex, "type": "text", "text": final_text}],
        "createdAt": completed_at,
    }


def get_conversation(cid: str) -> dict[str, Any] | None:
    try:
        from api.history import get_history

        return get_history(cid)
    except Exception:  # noqa: BLE001 - missing conversation -> None
        return None


def upsert_conversation(conv: dict[str, Any]) -> None:
    from api.history import HistoryPut, put_history

    put_history(conv["id"], HistoryPut(conversation=conv))


def append_timer_start(cid: str, task: dict[str, Any], trigger_text: str) -> None:
    """写「开始」：结构化 user 标记消息 + assistant 占位（status=pending 不存在——
    用 running 骨架，最终由 append_timer_end 覆盖）。"""
    conv = get_conversation(cid)
    if conv is None:
        conv = {
            "id": cid,
            "title": f"【定时任务】{(task.get('name') or '定时任务')}",
            "updatedAt": _now_ms(),
            "messages": [],
        }
    msgs = conv.get("messages") or []
    started_at = _now_ms()
    msgs.append(_make_timer_user_msg(task, trigger_text))
    msgs.append(
        {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "finalText": "",
            "status": "pending",
            "process": {
                "startedAt": started_at,
                "plan": [],
                "analyses": [],
                "toolCalls": [],
            },
            "segments": [],
            "createdAt": started_at,
        }
    )
    conv["messages"] = msgs
    conv["updatedAt"] = _now_ms()
    upsert_conversation(conv)


def append_timer_end(
    cid: str, final_text: str, status: str, assistant_message_id: str | None = None
) -> None:
    """写「结束」：用最终文本覆盖最后一条 assistant 占位。

    `assistant_message_id` 透传给消息 id，使 runHistory 的
    `assistantMessageId` 与会话消息 id 一致（前端执行记录可跳转到消息）。
    """
    conv = get_conversation(cid)
    if conv is None:
        return
    msgs = conv.get("messages") or []
    # 找到最后一条 assistant 且为空/占位 -> 覆盖；否则追加
    idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("status") in ("pending", "running"):
            idx = i
            break
    if idx >= 0:
        msgs[idx] = _make_assistant_msg(final_text, status, msgs[idx]["createdAt"], assistant_message_id)
    else:
        msgs.append(_make_assistant_msg(final_text, status, _now_ms(), assistant_message_id))
    conv["messages"] = msgs
    conv["updatedAt"] = _now_ms()
    upsert_conversation(conv)


def _extract_ai_text(messages: list[Any]) -> str:
    for m in reversed(messages):
        if getattr(m, "type", None) in ("ai", "AIMessage") or getattr(m, "role", "") == "assistant":
            content = getattr(m, "content", "") or ""
            if isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict):
                        parts.append(str(c.get("text", "") or ""))
                    else:
                        parts.append(str(c))
                return "".join(parts)
            return str(content)
    return ""


# ---- 执行 ----
async def run_timer_task(task: dict[str, Any], trigger_text: str) -> dict[str, Any]:
    """执行单个任务并写回履历。返回本轮 run 记录（含 status）。"""
    session_id = task.get("agentSessionId") or task.get("id")
    cid = task.get("sessionId") or ""
    started_at = _now_ms()
    assistant_message_id = uuid.uuid4().hex
    run: dict[str, Any] = {
        "id": f"run_{uuid.uuid4().hex[:8]}",
        "startedAt": started_at,
        "status": "running",
        "assistantMessageId": assistant_message_id,
    }

    if cid:
        try:
            append_timer_start(cid, task, trigger_text)
        except Exception:  # noqa: BLE001 - history write must never break execution
            logger.exception("[timers] write start to session failed cid=%s", cid)

    server, conn = await _get_server()
    # 注册 session 元数据（_session_config 依赖 _session_cwds）
    server._session_cwds[session_id] = str(_PROJECT_ROOT)  # noqa: SLF001
    server._session_modes[session_id] = "auto"  # noqa: SLF001
    # 按任务指定 agent（build_agent 的 agent_name 覆盖；未指定则 default）
    agent_name = (task.get("agent") or "default").strip() or "default"
    server._session_agents[session_id] = agent_name  # noqa: SLF001
    logger.info("[timers] run task=%s agent=%s session=%s", task.get("id"), agent_name, session_id)
    conn.permission_denied = False

    final_text = ""
    status = "success"
    try:
        from acp.helpers import text_block

        result = await server.prompt(
            [text_block(task.get("prompt", ""))],
            session_id=session_id,
        )
        try:
            config = server._session_config(session_id)
            state = await server._agent.aget_state(config)
            values = getattr(state, "values", None) or state
            final_text = _extract_ai_text((values or {}).get("messages", []) or [])
        except Exception:  # noqa: BLE001 - result extraction must not fail the run
            logger.exception("[timers] extract final text failed session=%s", session_id)
        if conn.permission_denied:
            status = "failed"
            if final_text:
                final_text = f"{final_text}\n\n（无人值守：权限请求被自动拒绝，任务记为 failed）"
            else:
                final_text = "（无人值守：权限请求被自动拒绝，任务记为 failed）"
        elif result is not None and getattr(result, "stop_reason", None) == "cancelled":
            status = "cancelled"
            final_text = final_text or "（任务被取消）"
    except Exception as e:  # noqa: BLE001 - a failing run is recorded, not raised
        logger.exception("[timers] task failed task=%s", task.get("id"))
        status = "failed"
        final_text = f"执行异常：{e}"

    finished_at = _now_ms()
    run.update(
        {
            "finishedAt": finished_at,
            "status": status,
            "outputSummary": (final_text or "")[:500],
        }
    )

    if cid:
        try:
            append_timer_end(
                cid,
                final_text,
                "completed" if status == "success" else "failed",
                assistant_message_id=assistant_message_id,
            )
        except Exception:  # noqa: BLE001 - history write must never fail the run
            logger.exception("[timers] write end to session failed cid=%s", cid)

    # 回写任务记录
    task["lastRunAt"] = finished_at
    task["lastStatus"] = status
    task["runHistory"] = [*list(task.get("runHistory") or []), run][-50:]
    task["updatedAt"] = finished_at
    try:
        save_task(task)
    except Exception:  # noqa: BLE001 - persistence must never fail the response
        logger.exception("[timers] persist task state failed task=%s", task.get("id"))
    return run


# ---- 调度 ----
def _normalize_cron(expr: str) -> str:
    """把用户可能粘贴的 Quartz 风格表达式转成标准 5 段 cron（croniter 用）。

    - Quartz 6 段（秒 分 时 日 月 周，如 `0 30 16 * * ?`）：丢弃秒段 → 5 段；
    - `?` / `*?`（无特定值，Quartz 的 day-of-month/day-of-week）等价 `*`：
      逐段去掉 `?`，空段按 `*` 处理。
    """
    parts = expr.strip().split()
    if len(parts) == 6:
        parts = parts[1:]
    norm = [(p.replace("?", "") or "*") for p in parts]
    return " ".join(norm)


def normalize_cron_safe(expr: str) -> str:
    """规范化并校验 cron：合法返回标准 5 段表达式，非法返回空串。"""
    expr = (expr or "").strip()
    if not expr:
        return ""
    norm = _normalize_cron(expr)
    if norm.count(" ") != 4:  # 必须为 5 段
        return ""
    try:
        croniter(norm, datetime.now()).get_next(datetime)
    except Exception:  # noqa: BLE001 - invalid field values / ranges
        return ""
    return norm


def _cron_field(field: str, names: list[str]) -> str:
    """把单段 cron 字段翻译成人话；无法解析时原样返回。"""
    if field in ("*", ""):
        return "*"
    if field.startswith("*/"):
        n = field[2:]
        if n.isdigit():
            return f"每 {n} {names[-1]}"
        return field
    if field.isdigit():
        return names[int(field) % len(names)] if names[0] in ("周日",) else f"{int(field)}{names[-1]}"
    if "-" in field and field.split("-")[0].isdigit() and field.split("-")[1].isdigit():
        a, b = field.split("-")
        return f"{a}-{b}"
    return field


def cron_description_local(norm: str) -> str:
    """本地确定性翻译：标准 5 段 cron → 中文自然语言（AI 不可用时的降级）。"""
    try:
        parts = norm.split()
        if len(parts) != 5:
            return ""
        m, h, dom, mon, dow = parts
        dow_names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        is_all = lambda v: v in ("*", "?")  # noqa: E731
        time_text = ""
        if m.startswith("*/") and is_all(h):
            time_text = f"每 {m[2:]} 分钟"
        elif h.startswith("*/") and m == "0":
            time_text = f"每 {h[2:]} 小时"
        elif is_all(m) and is_all(h):
            time_text = "每分钟"
        elif h.isdigit() and m.isdigit():
            time_text = f"{h.zfill(2)}:{m.zfill(2)}"
        else:
            time_text = f"{h if not is_all(h) else ''}时{m if not is_all(m) else ''}分"
        if not is_all(dom) and not is_all(dow):
            mon_text = "" if is_all(mon) else f"{mon} 月"
            return f"每年{mon_text} {dom} 日 {time_text}".replace("  ", " ")
        if not is_all(dom):
            return f"每月 {dom} 日 {time_text}"
        if not is_all(dow):
            dow_text = ""
            if dow.isdigit():
                dow_text = dow_names[int(dow) % 7]
            elif "-" in dow and dow.split("-")[0].isdigit() and dow.split("-")[1].isdigit():
                a, b = dow.split("-")
                dow_text = f"{dow_names[int(a) % 7]}至{dow_names[int(b) % 7]}"
            else:
                dow_text = dow
            return f"每周{dow_text} {time_text}"
        if not is_all(mon):
            return f"每年 {mon} 月 {time_text}"
        return f"每天 {time_text}"
    except Exception:  # noqa: BLE001 - never break explain
        return ""


async def explain_cron_ai(norm: str) -> str:
    """AI 生成 cron 自然语言描述；模型不可用/超时降级为本地翻译。"""
    local = cron_description_local(norm)
    try:
        from langchain_core.messages import HumanMessage
        from utils.model_util import MODEL

        prompt = (
            "把下面这个标准 5 段 cron 表达式翻译成一句简洁的中文自然语言描述"
            "（不要解释字段含义，直接说‘每天 09:00 触发’这类一句话）。\n"
            f"cron: {norm}\n"
            "只输出描述本身，不要多余内容。"
        )
        result = await asyncio.wait_for(
            MODEL.ainvoke([HumanMessage(content=prompt)]), timeout=8
        )
        text = getattr(result, "content", "") or ""
        if isinstance(text, list):
            text = "".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in text)
        text = str(text).strip().strip('"').strip()
        if text and len(text) <= 120:
            return text
    except Exception:  # noqa: BLE001 - fall back to deterministic description
        logger.warning("[timers] AI cron explain failed, fallback local: %s", norm)
    return local


def next_run_ms(schedule: str, now: datetime) -> int | None:
    """计算下次触发时间（毫秒）；非法表达式返回 None。"""
    try:
        return int(croniter(_normalize_cron(schedule), now).get_next(datetime).timestamp() * 1000)
    except Exception:  # noqa: BLE001 - invalid cron is surfaced as None
        return None


def _is_due(task: dict[str, Any], now: datetime, tick_seconds: int) -> bool:
    schedule = (task.get("schedule") or "").strip()
    interval = task.get("intervalMinutes")
    last = task.get("lastRunAt")
    if schedule:
        try:
            prev = croniter(_normalize_cron(schedule), now).get_prev(datetime)
        except Exception:  # noqa: BLE001 - malformed cron never breaks the tick
            logger.warning("[timers] invalid cron %r task=%s (5-field standard cron expected)", schedule, task.get("id"))
            return False
        window = now - timedelta(seconds=max(tick_seconds * 2, 30))
        return prev >= window and (last is None or last < int(prev.timestamp() * 1000))
    if interval:
        now_ms = int(now.timestamp() * 1000)
        if last is None:
            return True  # 首次：立即触发一次
        return now_ms >= int(last) + int(interval) * 60000
    return False


async def tick() -> None:
    """一轮调度：扫描 enabled 任务，命中则执行（串行）。"""
    if os.getenv("TIMER_ENABLED", "true").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        tick_seconds = int(os.getenv("TIMER_TICK_SECONDS", "30").strip() or "30")
    except ValueError:
        tick_seconds = 30
    now = datetime.now()
    tasks = list_tasks(enabled_only=True)
    for task in tasks:
        if task.get("lastStatus") == "running":
            continue  # 上一轮未结束（D9 任务级互斥，另有内存 _running 兜底）
        if not _is_due(task, now, tick_seconds):
            continue
        try:
            name = task.get("name", "定时任务")
            trigger_text = f"自动触发 · {now.strftime('%Y-%m-%d %H:%M:%S')}"
            await run_timer_task(task, trigger_text)
        except Exception:  # noqa: BLE001 - one failing task never stops the loop
            logger.exception("[timers] tick task failed task=%s", task.get("id"))


async def scheduler_loop() -> None:
    """常驻调度循环（lifespan 启动；禁用时直接返回）。"""
    if os.getenv("TIMER_ENABLED", "true").strip().lower() not in ("1", "true", "yes", "on"):
        logger.info("[timers] TIMER_ENABLED=false, scheduler not started")
        return
    try:
        tick_seconds = int(os.getenv("TIMER_TICK_SECONDS", "30").strip() or "30")
    except ValueError:
        tick_seconds = 30
    logger.info("[timers] scheduler started (tick=%ss)", tick_seconds)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001 - the loop must survive any tick error
            logger.exception("[timers] scheduler tick error")
        await asyncio.sleep(tick_seconds)


def recover_running() -> None:
    """重启恢复（D10）：把遗留 running 记录补 cancelled。"""
    try:
        tasks = list_tasks()
    except Exception:  # noqa: BLE001 - recovery must never break startup
        return
    for task in tasks:
        if task.get("lastStatus") == "running":
            task["lastStatus"] = "cancelled"
            history = list(task.get("runHistory") or [])
            if history and history[-1].get("status") == "running":
                history[-1]["status"] = "cancelled"
                history[-1]["finishedAt"] = _now_ms()
                history[-1]["outputSummary"] = "（服务重启，任务被取消）"
            task["runHistory"] = history
            task["updatedAt"] = _now_ms()
            try:
                save_task(task)
            except Exception:  # noqa: BLE001 - per-task recovery is best-effort
                logger.exception("[timers] recover task failed task=%s", task.get("id"))
