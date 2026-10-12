"""Manual context compaction API.

POST /api/context/compact
    body: {"sessionId": "<acp thread_id>", "agent": "<agent_name>"}

Builds a deep agent on the shared checkpoint DB, compacts the session's
message history (summarize early messages + RemoveMessage), and returns the
estimated token usage before/after so the frontend can refresh its display.
"""

import asyncio
import logging
import os

import aiosqlite
from fastapi import APIRouter
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import acp_agent as _acp
from utils.context_compactor import (
    CONTENT_SIZE,
    estimate_tokens,
    compact_messages,
)
from utils.mcp_loader import close_mcp_clients

logger = logging.getLogger("api.context")
router = APIRouter()


async def _compact(session_id: str, agent_name: str) -> dict:
    os.environ["AGENT_NAME"] = agent_name or "default"
    conn = await aiosqlite.connect(_acp.DB_PATH)
    checkpointer = AsyncSqliteSaver(conn)
    agent = None
    try:
        context = _acp.AgentSessionContext(cwd=".", model=None)
        agent = _acp.build_agent(context, checkpointer)
        config = {"configurable": {"thread_id": session_id}}
        state = await agent.aget_state(config)
        values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
        messages = list((values or {}).get("messages", []) or [])
        if not messages:
            return {"ok": False, "error": "empty_session"}
        before_tokens = estimate_tokens(messages)
        result = await compact_messages(agent, config, messages)
        # Re-read to report the post-compaction estimate.
        state2 = await agent.aget_state(config)
        values2 = state2.values if hasattr(state2, "values") else (state2 if isinstance(state2, dict) else {})
        after_tokens = estimate_tokens(list((values2 or {}).get("messages", []) or []))
        result.update({
            "ok": True,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "pct_before": round(100 * before_tokens / CONTENT_SIZE),
            "pct_after": round(100 * after_tokens / CONTENT_SIZE),
        })
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("compact failed for session=%s", session_id)
        return {"ok": False, "error": str(e)}
    finally:
        await close_mcp_clients()
        await conn.close()


@router.post("/api/context/compact")
async def compact_context(body: dict):
    session_id = (body or {}).get("sessionId")
    agent_name = (body or {}).get("agent") or "default"
    if not session_id:
        return {"ok": False, "error": "missing sessionId"}
    # build_agent / MCP client startup is blocking and CPU-light but slow;
    # run it in a worker thread to avoid stalling the event loop.
    return await asyncio.to_thread(_compact, session_id, agent_name)
