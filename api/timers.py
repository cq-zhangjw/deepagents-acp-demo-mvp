"""定时任务 API：任务 CRUD + 立即执行 + 执行履历。

设计文档：docs/schedule_task.md 阶段二（§4.6）。
存储与执行见 `api/timers_engine.py`（MooFile `db_timer_tasks_<hash8>`）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import api.timers_engine as engine

router = APIRouter(prefix="/api/timers", tags=["timers"])


def _new_task_id() -> str:
    return f"timer_{uuid.uuid4().hex[:12]}"


def _new_cid() -> str:
    return f"timer-{uuid.uuid4().hex[:10]}"


def _ensure_task(task_id: str) -> dict[str, Any]:
    task = engine.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="timer task not found")
    return task


class TimerPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    prompt: str = Field(min_length=1, max_length=8000)
    schedule: str | None = None
    intervalMinutes: int | None = Field(default=None, ge=1, le=10080)
    sessionId: str | None = None
    agent: str | None = None
    enabled: bool = True


class TimerUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    schedule: str | None = None
    intervalMinutes: int | None = None
    sessionId: str | None = None
    agent: str | None = None
    enabled: bool | None = None


class CronExplainPayload(BaseModel):
    cron: str


def _validate_schedule(schedule: str | None) -> str | None:
    """校验 cron 表达式：非法返回 None（同时规范化）。"""
    if schedule is None:
        return None
    norm = engine.normalize_cron_safe(schedule)
    if not norm:
        raise HTTPException(status_code=400, detail="cron 表达式无效，请使用标准 5 段格式（如 30 16 * * *）")
    return norm


@router.get("")
def list_timers() -> list[dict[str, Any]]:
    """任务列表（最新在前），附下次触发时间与 cron 有效性。"""
    tasks = engine.list_tasks()
    now = datetime.now()
    for t in tasks:
        sched = (t.get("schedule") or "").strip()
        if sched:
            nxt = engine.next_run_ms(sched, now)
            t["nextRunAt"] = nxt
            t["cronValid"] = nxt is not None
        else:
            t["cronValid"] = True
    return tasks


@router.post("")
def create_timer(payload: TimerPayload) -> dict[str, Any]:
    """新建任务：默认自动创建专用会话（写入 history，侧栏可见）。"""
    if payload.schedule and payload.intervalMinutes:
        raise HTTPException(status_code=400, detail="schedule 与 intervalMinutes 只能二选一")
    if not payload.schedule and not payload.intervalMinutes:
        raise HTTPException(status_code=400, detail="schedule 与 intervalMinutes 至少填一个")
    schedule = _validate_schedule(payload.schedule)

    task_id = _new_task_id()
    cid = payload.sessionId or _new_cid()
    agent_session_id = uuid.uuid4().hex

    # 专用会话：首次执行前就占位建好，保证侧栏立即可见
    conv = engine.get_conversation(cid)  # noqa: SLF001
    if conv is None:
        now = int(datetime.now().timestamp() * 1000)
        conv = {
            "id": cid,
            "agentSessionId": agent_session_id,
            "title": payload.name,
            "updatedAt": now,
            "messages": [],
        }
        engine.upsert_conversation(conv)  # noqa: SLF001

    now_ms = int(datetime.now().timestamp() * 1000)
    task: dict[str, Any] = {
        "id": task_id,
        "name": payload.name,
        "description": (payload.description or "").strip(),
        "prompt": payload.prompt,
        "schedule": schedule,
        "intervalMinutes": payload.intervalMinutes,
        "sessionId": cid,
        "agentSessionId": agent_session_id,
        "agent": (payload.agent or "default").strip() or "default",
        "enabled": payload.enabled,
        "lastRunAt": None,
        "lastStatus": "never",
        "runHistory": [],
        "createdAt": now_ms,
        "updatedAt": now_ms,
    }
    engine.save_task(task)
    return task


@router.put("/{task_id}")
def update_timer(task_id: str, payload: TimerUpdate) -> dict[str, Any]:
    """更新任务（名称 / prompt / 调度 / 启停 / 绑定会话）。"""
    task = _ensure_task(task_id)
    if payload.schedule is not None:
        task["schedule"] = _validate_schedule(payload.schedule)
    if payload.intervalMinutes is not None:
        task["intervalMinutes"] = payload.intervalMinutes
    if payload.schedule is None and payload.intervalMinutes is None:
        pass  # 未指定调度字段则保留
    if payload.name is not None:
        task["name"] = payload.name
    if payload.description is not None:
        task["description"] = (payload.description or "").strip()
    if payload.prompt is not None:
        task["prompt"] = payload.prompt
    if payload.sessionId is not None:
        task["sessionId"] = payload.sessionId
    if payload.agent is not None:
        task["agent"] = (payload.agent or "default").strip() or "default"
    if payload.enabled is not None:
        task["enabled"] = payload.enabled
    task["updatedAt"] = int(datetime.now().timestamp() * 1000)
    engine.save_task(task)
    return task


@router.delete("/{task_id}")
def delete_timer(task_id: str) -> dict[str, Any]:
    """删除任务（解绑会话，会话保留）。"""
    _ensure_task(task_id)
    engine.delete_task(task_id)
    return {"ok": True}


@router.post("/{task_id}/run")
async def run_timer(task_id: str) -> dict[str, Any]:
    """手动立即执行一次（同步等待结果返回）。"""
    task = _ensure_task(task_id)
    trigger_text = "手动触发"
    run = await engine.run_timer_task(task, trigger_text)
    return {"ok": True, "run": run}


@router.post("/explain-cron")
async def explain_cron(payload: CronExplainPayload) -> dict[str, Any]:
    """解析 cron 表达式：返回有效性 + 自然语言描述（AI 优先，本地兜底）。"""
    norm = engine.normalize_cron_safe(payload.cron)
    if not norm:
        return {"valid": False, "normalized": None, "description": ""}
    desc = await engine.explain_cron_ai(norm)
    return {"valid": True, "normalized": norm, "description": desc}


@router.get("/{task_id}/history")
def timer_history(task_id: str) -> list[dict[str, Any]]:
    """执行履历（最近 50 条）。"""
    task = _ensure_task(task_id)
    return list(task.get("runHistory") or [])
