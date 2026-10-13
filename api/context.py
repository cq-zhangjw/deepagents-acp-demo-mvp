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


def _usage_sync(session_id: str, agent_name: str) -> dict:
    """Run the async _usage on a fresh event loop inside a worker thread."""
    return asyncio.run(_usage(session_id, agent_name))


def _compact_sync(session_id: str, agent_name: str) -> dict:
    return asyncio.run(_compact(session_id, agent_name))


async def _compact(session_id: str, agent_name: str) -> dict:
    os.environ["AGENT_NAME"] = agent_name or "default"
    conn = await aiosqlite.connect(_acp.DB_PATH)
    checkpointer = AsyncSqliteSaver(conn)
    agent = None
    try:
        context = _acp.AgentSessionContext(cwd=".", mode="default", model=None)
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


async def _usage(session_id: str, agent_name: str) -> dict:
    """Read the real model-side context usage from the agent checkpoint.

    The frontend estimates usage from the full conversation history, which
    stays untouched by compaction; this endpoint reports the true token usage
    of the checkpoint messages (already-compacted when compaction ran).
    """
    os.environ["AGENT_NAME"] = agent_name or "default"
    conn = await aiosqlite.connect(_acp.DB_PATH)
    checkpointer = AsyncSqliteSaver(conn)
    try:
        context = _acp.AgentSessionContext(cwd=".", mode="default", model=None)
        agent = _acp.build_agent(context, checkpointer)
        config = {"configurable": {"thread_id": session_id}}
        state = await agent.aget_state(config)
        values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
        messages = list((values or {}).get("messages", []) or [])
        tokens = estimate_tokens(messages) if messages else 0
        return {
            "ok": True,
            "tokens": tokens,
            "total": CONTENT_SIZE,
            "pct": round(100 * tokens / CONTENT_SIZE),
            "message_count": len(messages),
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("usage failed for session=%s", session_id)
        return {"ok": False, "error": str(e)}
    finally:
        await close_mcp_clients()
        await conn.close()


@router.get("/api/context/usage")
async def context_usage(sessionId: str, agent: str = "default"):
    if not sessionId:
        return {"ok": False, "error": "missing sessionId"}
    # _usage is async; run it on a fresh loop inside a worker thread so the
    # blocking build_agent / MCP startup cannot stall the uvicorn event loop.
    return await asyncio.to_thread(_usage_sync, sessionId, agent)


@router.post("/api/context/compact")
async def compact_context(body: dict):
    session_id = (body or {}).get("sessionId")
    agent_name = (body or {}).get("agent") or "default"
    if not session_id:
        return {"ok": False, "error": "missing sessionId"}
    # build_agent / MCP client startup is blocking and CPU-light but slow;
    # run it in a worker thread to avoid stalling the event loop.
    return await asyncio.to_thread(_compact_sync, session_id, agent_name)
