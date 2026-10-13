"""Context compaction for long ACP sessions.

When the model-input tokens of a session approach the model context window,
the earliest messages are summarized into a compact summary message and
removed from the checkpoint state, so the conversation can continue without
hitting the model's hard context limit.

The compaction runs:
- automatically: acp_agent.py subclasses AgentServerACP and checks before every
  session/prompt (threshold: CONTEXT_COMPACT_RATIO * CONTENT_SIZE);
- manually: POST /api/context/compact in api/context.py forces a compaction.
"""

import os

from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage

from utils.model_util import MODEL

# Context window size (tokens), same as the frontend display value.
CONTENT_SIZE = int(os.getenv("CONTENT_SIZE") or 32768)
# Compaction triggers when estimated tokens >= CONTENT_SIZE * RATIO.
COMPACT_RATIO = float(os.getenv("CONTEXT_COMPACT_RATIO") or 0.85)
# Extra tokens not counted by the message estimator (system prompt, tool
# schemas, images, ...). Keeps the check conservative.
COMPACT_OVERHEAD = int(os.getenv("CONTEXT_COMPACT_OVERHEAD") or 6000)
# Maximum tokens the summarizer may produce.
SUMMARY_MAX_TOKENS = int(os.getenv("CONTEXT_COMPACT_SUMMARY_MAX_TOKENS") or 2048)
# Keep the most recent N messages untouched; only earlier ones are summarized.
KEEP_RECENT = int(os.getenv("CONTEXT_COMPACT_KEEP_RECENT") or 8)
# Summarizer input guard: at most ~this fraction of the window (in chars,
# roughly tokens * 1.7) is sent to the model for summarizing.
SUMMARY_INPUT_FRACTION = float(os.getenv("CONTEXT_COMPACT_SUMMARY_INPUT_FRACTION") or 0.6)


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """Rough token estimate (chars / 1.7), consistent with the frontend.

    Counts message text plus tool-call arguments (no images / attachments).
    """
    chars = 0
    for m in messages or []:
        content = getattr(m, "content", "")
        if isinstance(content, str):
            chars += len(content)
        else:
            chars += len(str(content))
        for tc in getattr(m, "tool_calls", None) or []:
            chars += len(str(tc.get("args", "") if isinstance(tc, dict) else getattr(tc, "args", "")))
    return max(1, round(chars / 1.7))


def _msg_text(m: BaseMessage) -> str:
    """Extract plain text from a message content (str or content blocks)."""
    content = getattr(m, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            elif isinstance(block, BaseMessage):
                parts.append(_msg_text(block))
        return "\n".join(parts)
    return str(content)


def _role_label(m: BaseMessage) -> str:
    return type(m).__name__.replace("Message", "").lower() or "message"


def format_messages_for_summary(messages: list[BaseMessage]) -> str:
    """Render messages as labeled lines for the summarizer prompt."""
    lines = []
    for m in messages:
        text = _msg_text(m)
        if not text.strip():
            continue
        lines.append(f"[{_role_label(m)}] {text}")
    return "\n\n".join(lines)


async def build_summary(messages: list[BaseMessage]) -> str:
    """Ask the model to summarize the given messages into a compact summary.

    The input text is guarded so the summarizer prompt itself stays well below
    the context window. Returns the summary text.
    """
    text = format_messages_for_summary(messages)
    max_chars = max(2000, int(CONTENT_SIZE * SUMMARY_INPUT_FRACTION * 1.7))
    if len(text) > max_chars:
        # Keep the head (task goal, early steps) and the tail (latest progress).
        head = text[: max_chars // 2]
        tail = text[-max_chars // 2:]
        text = f"{head}\n\n[... omitted {len(text) - max_chars} chars ...]\n\n{tail}"

    prompt = (
        "You are compressing a long conversation history to fit the AI agent's context window. "
        "Preserve: the original task goal, all important facts and decisions, completed steps and "
        "their results, current progress, and any open questions or errors still being investigated. "
        "Write a compact summary in the same language as the conversation. "
        f"Do not exceed {SUMMARY_MAX_TOKENS} tokens.\n\n"
        f"<conversation>\n{text}\n</conversation>"
    )
    resp = await MODEL.ainvoke([HumanMessage(content=prompt)])
    content = resp.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        text = "\n".join(parts).strip()
        return text or "[summary unavailable]"
    return str(content).strip() or "[summary unavailable]"


def split_compact(messages: list[BaseMessage]):
    """Split messages into (early-to-summarize, recent-to-keep)."""
    if len(messages) <= KEEP_RECENT + 1:
        return [], list(messages)
    return list(messages[:-KEEP_RECENT]), list(messages[-KEEP_RECENT:])


async def compact_messages_list(messages: list[BaseMessage]) -> tuple[list[BaseMessage], dict]:
    """Pure-function compaction of a message list.

    Summarizes the early messages, drops them, and returns
    ``(compacted_list, result_dict)``. The caller decides where the compacted
    list is written (session_content table, checkpoint, or an API response), so
    the same logic serves the table-authoritative flow and the legacy path.
    """
    before_tokens = estimate_tokens(messages)
    early, recent = split_compact(messages)
    if not early:
        return list(messages), {
            "ok": False,
            "reason": "not_enough_messages",
            "before_tokens": before_tokens,
        }

    summary_text = await build_summary(early)
    compacted = [HumanMessage(content=summary_text), *recent]
    return compacted, {
        "ok": True,
        "summary": summary_text[:200],
        "removed": len(early),
        "kept": len(recent),
        "before_tokens": before_tokens,
    }


async def compact_messages(agent, config, messages: list[BaseMessage]) -> dict:
    """Compact a session's message history in the checkpoint (legacy path).

    Summarizes the early messages, removes them, and appends the summary as a
    HumanMessage. Returns a result dict for logging / the manual API.
    """
    compacted, result = await compact_messages_list(messages)
    if not result.get("ok"):
        return result

    removals = [RemoveMessage(id=m.id) for m in messages if getattr(m, "id", None)]
    summary_msg = compacted[0]
    await agent.aupdate_state(
        config,
        {"messages": [*removals, summary_msg]},
        as_node="__start__",
    )
    return result


def should_compact(messages: list[BaseMessage]) -> bool:
    """Threshold check: estimated tokens + overhead >= CONTENT_SIZE * RATIO."""
    if not messages:
        return False
    tokens = estimate_tokens(messages) + COMPACT_OVERHEAD
    return tokens >= CONTENT_SIZE * COMPACT_RATIO
