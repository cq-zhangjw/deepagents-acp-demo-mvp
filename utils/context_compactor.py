"""Context compaction for long ACP sessions.

When the model-input tokens of a session approach the model context window,
the earliest messages are summarized into a compact summary message and
removed from the checkpoint state, so the conversation can continue without
hitting the model's hard context limit.

The compaction runs:
- automatically: acp_agent.py subclasses AgentServerACP and checks before every
  session/prompt (threshold: CONTEXT_COMPACT_RATIO * CONTENT_SIZE);
- manually: POST /api/context/compact in api/context.py forces a compaction.

Optimization (see docs/context-compaction-optimization.md):
- rolling summary: previous summaries are fully inherited, never rewritten from
  scratch (no round-by-round information decay);
- task-goal anchor: the earliest user message survives compaction verbatim;
- structured summary template: forces target / progress / absolute paths /
  decisions / open items to be listed explicitly;
- chunked summarization replaces head+tail truncation when the input is long.
"""

import os

from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage

from utils.model_util import create_model, current_content_size

# Context window size (tokens), same as the frontend display value.
# Source: model_config.json -> the model selected by the frontend (WS query
# param -> env MODEL_NAME, same pattern as AGENT_NAME).
CONTENT_SIZE = current_content_size() or 32768
# Compaction triggers when estimated tokens >= CONTENT_SIZE * RATIO.
COMPACT_RATIO = float(os.getenv("CONTEXT_COMPACT_RATIO") or 0.80)
# Extra tokens not counted by the message estimator (system prompt, tool
# schemas, images, ...). Keeps the check conservative.
COMPACT_OVERHEAD = int(os.getenv("CONTEXT_COMPACT_OVERHEAD") or 6000)
# Maximum tokens the summarizer may produce.
SUMMARY_MAX_TOKENS = int(os.getenv("CONTEXT_COMPACT_SUMMARY_MAX_TOKENS") or 2048)
# Keep the most recent N messages untouched; only earlier ones are summarized.
KEEP_RECENT = int(os.getenv("CONTEXT_COMPACT_KEEP_RECENT") or 12)
# Summarizer input guard: at most ~this fraction of the window (in chars,
# roughly tokens * 1.7) is sent to the model for summarizing.
SUMMARY_INPUT_FRACTION = float(os.getenv("CONTEXT_COMPACT_SUMMARY_INPUT_FRACTION") or 0.6)

# Marker for summary messages: metadata key + content prefix (dual signal so the
# summary survives even if metadata is dropped by a storage layer).
SUMMARY_ROLE = "__role__"
SUMMARY_ROLE_VALUE = "context_summary"
SUMMARY_PREFIX = "【历史上下文摘要】"


def _is_summary(m: BaseMessage) -> bool:
    """True if the message is a compaction summary (metadata or prefix)."""
    meta = getattr(m, "metadata", None) or {}
    if isinstance(meta, dict) and meta.get(SUMMARY_ROLE) == SUMMARY_ROLE_VALUE:
        return True
    text = _msg_text(m)
    return bool(text and text.lstrip().startswith(SUMMARY_PREFIX))


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


def _summary_sections(is_first: bool) -> list[str]:
    if is_first:
        return [
            "目标：",
            "已完成：",
            "当前进度：",
            "关键事实与绝对路径：",
            "已做决策：",
            "待办与未解决：",
        ]
    return [
        "新增关键事实与绝对路径：",
        "新增已做决策：",
        "待办与未解决：",
    ]


def _build_prompt(text: str, prior_summary: str | None = None, is_first: bool = True) -> str:
    sections = "\n".join(f"- {s}" for s in _summary_sections(is_first))
    prompt = (
        "You are compressing a long conversation history to fit the AI agent's context window. "
        "Output a compact summary with EXACTLY these sections (same language as the conversation):\n"
        f"{sections}\n"
        "- 关键事实与绝对路径 section MUST list every absolute path, file name, "
        "data value and conclusion; omitting one is a failure.\n"
        "- Do not exceed %d tokens.\n\n" % SUMMARY_MAX_TOKENS
    )
    if prior_summary:
        prompt += (
            "CRITICAL: The following is the PREVIOUS summary of the earlier conversation. "
            "You MUST fully preserve every point it contains — do not drop, rewrite or shorten it. "
            "Only add new information from the new conversation chunk below.\n\n"
            f"<previous-summary>\n{prior_summary}\n</previous-summary>\n\n"
        )
    prompt += f"<conversation>\n{text}\n</conversation>"
    return prompt


def _extract_text(resp) -> str:
    content = resp.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts).strip()
    return str(content).strip()


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split long text into chunks of at most max_chars chars (on paragraph gaps)."""
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > max_chars and cur:
            chunks.append(cur.strip())
            cur = para
        else:
            cur = (cur + "\n\n" + para if cur else para)
        # 单个段落仍超限：硬切
        while len(cur) > max_chars:
            chunks.append(cur[:max_chars].strip())
            cur = cur[max_chars:]
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


async def build_summary(messages: list[BaseMessage], prior_summary: str | None = None) -> str:
    """Ask the model to summarize the given messages into a compact summary.

    The prior summary (if any) is passed verbatim and must be fully preserved
    (rolling accumulation). Long inputs are chunked and summarized piecewise so
    mid-conversation decisions are not truncated away. Returns the summary text.
    """
    text = format_messages_for_summary(messages)
    max_chars = max(2000, int(CONTENT_SIZE * SUMMARY_INPUT_FRACTION * 1.7))
    chunks: list[str] = []
    if prior_summary:
        chunks.append(f"[历史摘要]\n{prior_summary}")
    chunks.extend(_split_text(text, max_chars))
    if not chunks:
        return "[summary unavailable]"

    model = create_model()
    parts: list[str] = []
    for i, chunk in enumerate(chunks):
        is_first = i == 0
        prompt = _build_prompt(chunk, prior_summary=prior_summary if is_first else None, is_first=is_first)
        try:
            resp = await model.ainvoke([HumanMessage(content=prompt)])
            part = _extract_text(resp)
        except Exception:  # noqa: BLE001 - summarization must never break the prompt
            part = ""
        if part:
            parts.append(part)
    return "\n\n".join(parts).strip() or "[summary unavailable]"


def _task_anchor(messages: list[BaseMessage]) -> BaseMessage | None:
    """Earliest non-summary user message: the original task goal, kept verbatim."""
    for m in messages:
        if _is_summary(m):
            continue
        if isinstance(m, HumanMessage):
            return m
    return None


def split_compact(messages: list[BaseMessage]):
    """Split messages into (early-to-summarize, to-keep).

    - summary messages are never removed (their content is inherited by the new
      summary instead);
    - the task-goal anchor (earliest user message) is never removed;
    - the most recent KEEP_RECENT messages are kept untouched.
    """
    if not messages:
        return [], list(messages)
    summaries = [m for m in messages if _is_summary(m)]
    others = [m for m in messages if not _is_summary(m)]
    anchor = _task_anchor(others)

    if len(others) <= KEEP_RECENT:
        # 没有需要总结的早期消息：保留原样（含旧 summary）
        return [], list(messages)

    recent = others[-KEEP_RECENT:]
    early = [m for m in others[:-KEEP_RECENT] if m is not anchor]
    to_keep = summaries + ([anchor] if anchor is not None and anchor not in recent else []) + recent
    return early, to_keep


def _prior_summary_text(to_keep: list[BaseMessage]) -> str | None:
    """Return the concatenated previous summary text (if any) from to_keep."""
    texts = [_msg_text(m).replace(SUMMARY_PREFIX, "", 1).strip() for m in to_keep if _is_summary(m)]
    texts = [t for t in texts if t]
    return "\n\n".join(texts) if texts else None


async def compact_messages_list(messages: list[BaseMessage]) -> tuple[list[BaseMessage], dict]:
    """Pure-function compaction of a message list.

    Summarizes the early messages, drops them, and returns
    ``(compacted_list, result_dict)``. The caller decides where the compacted
    list is written (session_content table, checkpoint, or an API response), so
    the same logic serves the table-authoritative flow and the legacy path.
    """
    before_tokens = estimate_tokens(messages)
    early, to_keep = split_compact(messages)
    if not early:
        return list(messages), {
            "ok": False,
            "reason": "not_enough_messages",
            "before_tokens": before_tokens,
        }

    prior = _prior_summary_text(to_keep)
    summary_text = await build_summary(early, prior_summary=prior)
    summary_msg = HumanMessage(
        content=f"{SUMMARY_PREFIX}\n{summary_text}",
        metadata={SUMMARY_ROLE: SUMMARY_ROLE_VALUE},
    )
    # 旧 summary 已被新 summary 完整继承，避免重复
    compacted = [summary_msg] + [m for m in to_keep if not _is_summary(m)]
    return compacted, {
        "ok": True,
        "summary": summary_text[:200],
        "removed": len(early),
        "kept": len([m for m in compacted if not _is_summary(m)]),
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
