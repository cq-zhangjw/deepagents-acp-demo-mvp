"""Manual context control API (backed by the session-context table).

POST /api/context/compact
    body: {"sessionId": "<acp thread_id>", "agent": "<agent_name>"}

    Compacts the session's context messages (summarize early messages + drop
    them) against the MooFile context table, and returns the estimated token
    usage before/after so the frontend can refresh its display.

GET /api/context/usage?sessionId=...&agent=...
    Reports the model-side context usage based on the context table (the same
    source of truth the next prompt will actually feed the model), instead of
    the checkpoint.

Phase-2 endpoints (GET/PUT/DELETE message, rollback) extend this module using
utils/session_context.update_message / delete_message / rollback_last_turn.
"""

import asyncio
import logging
import os

import aiosqlite
from fastapi import APIRouter
from langchain_core.messages import convert_to_messages
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import acp_agent as _acp
from utils.context_compactor import (
    CONTENT_SIZE,
    estimate_tokens,
    compact_messages_list,
)
from utils.mcp_loader import close_mcp_clients
from utils.session_context import (
    delete_message as sc_delete_message,
    get_context as sc_get_context,
    init_from_checkpoint as sc_init_from_checkpoint,
    save_context as sc_save_context,
    update_message as sc_update_message,
)

logger = logging.getLogger("api.context")
router = APIRouter()


def _usage_sync(session_id: str, agent_name: str) -> dict:
    """Run the async _usage on a fresh event loop inside a worker thread."""
    return asyncio.run(_usage(session_id, agent_name))


def _compact_sync(session_id: str, agent_name: str) -> dict:
    return asyncio.run(_compact(session_id, agent_name))


async def _seed_from_checkpoint(session_id: str, agent, config) -> list[dict]:
    """Seed the context table from the checkpoint for a legacy session that has
    no table record yet; returns the stored to_json message list."""
    state = await agent.aget_state(config)
    values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
    ckpt_msgs = list((values or {}).get("messages", []) or [])
    return sc_init_from_checkpoint(session_id, ckpt_msgs)


async def _compact(session_id: str, agent_name: str) -> dict:
    os.environ["AGENT_NAME"] = agent_name or "default"
    conn = await aiosqlite.connect(_acp.DB_PATH)
    checkpointer = AsyncSqliteSaver(conn)
    agent = None
    try:
        context = _acp.AgentSessionContext(cwd=".", mode="default", model=None)
        agent = _acp.build_agent(context, checkpointer)
        config = {"configurable": {"thread_id": session_id}}

        messages_json = sc_get_context(session_id)
        if messages_json is None:
            # Legacy session without a table record: seed it once from checkpoint.
            messages_json = await _seed_from_checkpoint(session_id, agent, config)
        if not messages_json:
            return {"ok": False, "error": "empty_session"}

        objs = convert_to_messages(messages_json)
        before_tokens = estimate_tokens(objs)
        compacted, result = await compact_messages_list(objs)
        sc_save_context(session_id, compacted)
        after_tokens = estimate_tokens(compacted)
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
    """Report context usage from the context table.

    The table is the single source of truth for what the next prompt feeds the
    model, so its token estimate is the consistent display value (compaction
    against the table is reflected immediately). Legacy sessions without a
    table record fall back to the checkpoint and seed the table once.
    """
    os.environ["AGENT_NAME"] = agent_name or "default"
    messages_json = sc_get_context(session_id)
    if messages_json is None:
        # Legacy session: seed the table from the checkpoint so both displays
        # converge on the table.
        conn = await aiosqlite.connect(_acp.DB_PATH)
        checkpointer = AsyncSqliteSaver(conn)
        try:
            context = _acp.AgentSessionContext(cwd=".", mode="default", model=None)
            agent = _acp.build_agent(context, checkpointer)
            config = {"configurable": {"thread_id": session_id}}
            messages_json = await _seed_from_checkpoint(session_id, agent, config)
        except Exception as e:  # noqa: BLE001
            logger.exception("usage failed for session=%s", session_id)
            return {"ok": False, "error": str(e)}
        finally:
            await close_mcp_clients()
            await conn.close()

    objs = convert_to_messages(messages_json) if messages_json else []
    tokens = estimate_tokens(objs) if objs else 0
    return {
        "ok": True,
        "tokens": tokens,
        "total": CONTENT_SIZE,
        "pct": round(100 * tokens / CONTENT_SIZE),
        "message_count": len(objs),
    }


@router.get("/api/context/usage")
async def context_usage(sessionId: str, agent: str = "default"):
    if not sessionId:
        return {"ok": False, "error": "missing sessionId"}
    # _usage may build an agent (legacy seeding); run it in a worker thread so
    # the blocking build_agent / MCP startup cannot stall the uvicorn loop.
    return await asyncio.to_thread(_usage_sync, sessionId, agent)


@router.delete("/api/context/{session_id}")
def delete_context(session_id: str):
    """Delete a session everywhere it is persisted.

    Removes the context-table record (db/db_session_content_*) and the
    langgraph checkpoint thread (agent_state.sqlite), so no stale state
    survives. Idempotent: returns ok even if the session was already gone.
    """
    if not session_id:
        return {"ok": False, "error": "missing session_id"}
    from utils.session_context import delete_session

    try:
        removed = delete_session(session_id)
    except Exception:  # noqa: BLE001 - deletion must never 500 the gateway
        logger.exception("[session_context] delete failed for %s", session_id)
        return {"ok": False, "error": "delete failed"}
    return {"ok": True, "removed": removed}


@router.get("/api/context/{session_id}/messages")
async def list_context_messages(session_id: str):
    """Return the session's context-table messages (to_json list, each with
    kwargs.id). The frontend uses this to resolve its display messages to the
    ACP message ids required by PUT/DELETE /messages/{message_id}."""
    messages = sc_get_context(session_id)
    if messages is None:
        return {"ok": False, "error": "no_context_record"}
    return {"ok": True, "messages": messages}


@router.put("/api/context/{session_id}/messages/{message_id}")
async def edit_context_message(session_id: str, message_id: str, body: dict):
    """Edit one message in the session's context table.

    Two body shapes are accepted:
    - full message object (BaseMessage.to_json() shape); its kwargs.id must
      match `message_id` (or be absent, in which case it is patched in)
    - `{"content": "<new text>"}`: the backend locates the message by id and
      replaces only its content (the frontend cannot see to_json structures)

    The session's sqlite checkpoint mirror is invalidated so the edit takes
    effect on the next prompt.
    """
    if not body:
        return {"ok": False, "error": "missing message body"}
    target_id = str(body.get("kwargs", {}).get("id") or body.get("id") or "")
    if target_id and target_id != message_id:
        return {"ok": False, "error": "message_id mismatch"}
    if "content" in body and not target_id:
        # Simplified mode: replace content only. Rebuild a full to_json object
        # from the stored message so update_message keeps every other field.
        stored = sc_get_context(session_id) or []
        found = None
        for m in stored:
            mid = str((m.get("kwargs") or {}).get("id") or m.get("id") or "")
            if mid == message_id:
                found = m
                break
        if found is None:
            return {"ok": False, "error": "message not found or session missing"}
        import copy

        full = copy.deepcopy(found)
        (full.setdefault("kwargs", {}))["content"] = body["content"]
        ok = sc_update_message(session_id, full)
    else:
        if not target_id:
            body.setdefault("kwargs", {})["id"] = message_id
        try:
            ok = sc_update_message(session_id, body)
        except Exception:  # noqa: BLE001
            logger.exception(
                "edit message failed for session=%s message=%s", session_id, message_id
            )
            return {"ok": False, "error": "edit failed"}
    if not ok:
        return {"ok": False, "error": "message not found or session missing"}
    return {"ok": True, "updated": True}


@router.delete("/api/context/{session_id}/messages/{message_id}")
async def delete_context_message(session_id: str, message_id: str):
    """Delete one message from the session's context table.

    The session's sqlite checkpoint mirror is invalidated so the deletion takes
    effect on the next prompt.
    """
    try:
        ok = sc_delete_message(session_id, message_id)
    except Exception:  # noqa: BLE001
        logger.exception("delete message failed for session=%s message=%s", session_id, message_id)
        return {"ok": False, "error": "delete failed"}
    if not ok:
        return {"ok": False, "error": "message not found or session missing"}
    return {"ok": True, "removed": True}


@router.post("/api/context/compact")
async def compact_context(body: dict):
    session_id = (body or {}).get("sessionId")
    agent_name = (body or {}).get("agent") or "default"
    if not session_id:
        return {"ok": False, "error": "missing sessionId"}
    # build_agent / MCP client startup is blocking and CPU-light but slow;
    # run it in a worker thread to avoid stalling the event loop.
    return await asyncio.to_thread(_compact_sync, session_id, agent_name)
