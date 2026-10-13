"""DeepAgents ACP Agent.

Runs in standard ACP (Agent Client Protocol) stdio service mode:
- Reads ACP JSON-RPC requests from stdin (initialize / session/new / session/prompt ...)
- Automatically maps the agent's internal state to ACP events written to stdout:
    session/update              —— task progress, plans, tool calls, streaming text
    session/request_permission  —— permission approval (reading files/resources, risky operations)
    session/request_input       —— ask the user for missing information

Usage: python acp_agent.py
Note: The default model is openai:gpt-4o, so OPENAI_API_KEY must be set.
      MCP Server is not configured here; it is provided by the ACP client during new_session
      (see the README section on "MCP Mounting").
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import aiosqlite
from acp import run_agent
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend, StateBackend
from deepagents.backends.protocol import ExecuteResponse
from deepagents_acp.server import AgentServerACP, AgentSessionContext
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph

# Replace deepagents' upstream FilesystemMiddleware with the Windows
# real-filesystem override (utils/override/filesystem.py). It allows Windows
# absolute paths through the tool layer and resolves "/"-prefixed virtual
# paths under the working directory instead of the drive root. graph.py
# references the middleware by module-global name, so patching this attribute
# is enough to cover the main agent, subagents and the default GP subagent.
import deepagents.graph as _deepagents_graph
from utils.override.filesystem import FilesystemMiddleware as _OverrideFilesystemMiddleware

_deepagents_graph.FilesystemMiddleware = _OverrideFilesystemMiddleware

from deepagents.middleware.subagents import SubAgent
from utils.context_compactor import compact_messages, estimate_tokens, should_compact
from utils.model_util import MODEL
from utils.mcp_loader import close_mcp_clients, get_agent_mcp_tools, get_agent_subagents_config
from utils.skill_loader import get_agent_skills

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("acp_agent")

# Selected agent (passed by the gateway as AGENT_NAME env var; the frontend
# sends it through the /acp-ws?agent= query param). Its enabled MCP tools are
# loaded from .deepagents/agents/agents.json and attached in build_agent().
AGENT_NAME = os.getenv("AGENT_NAME", "default").strip() or "default"

# conversation state persistence file: checkpoints are saved (sqlite),
# survive process/gateway restarts; the frontend can load historical
# conversations via session/load and continue them.

DB_ROOT = os.getenv("DB_ROOT") if os.getenv("DB_ROOT") else "./db"
os.makedirs(DB_ROOT, exist_ok=True)
DB_PATH = os.path.join(DB_ROOT, "agent_state.sqlite")


# SYSTEM_PROMPT = (
#     "You are an intelligent task agent.\n"
#     "1. Break down the task, produce an execution plan, and run it step by step.\n"
#     "2. When essential information is missing, proactively ask the user (request_input); do not fabricate.\n"
#     "3. Image attachments are provided as image content; analyze them directly, do not call read_file. "
#     "Other attachments are provided as virtual paths (e.g. /uploads/example.txt); "
#     "read them with local file tools, do not treat them as http resources or Windows paths.\n"
#     "4. Request permission (request_permission) before high-risk tool operations.\n"
#     "5. Always report back to the user when the whole task is finished."
# )



def _load_agent_md(agent_name: str) -> str:
    """Load .deepagents/agents/<agent_name>.agent.md and strip the YAML front matter."""
    md_path = os.path.join(".deepagents", "agents", f"{agent_name}.agent.md")
    if not os.path.exists(md_path):
        return ""
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    # Strip YAML front matter (--- ... ---)
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].lstrip("\n")
    return content.strip()


def get_system_prompt() -> str:
    """Return the system prompt for the agent, appending agent md content when available."""
    if os.path.exists("./AGENTS.md"):
        with open("./AGENTS.md", "r", encoding="utf-8") as f:
            base_prompt = f.read()
    else:
        base_prompt = (
            "You are an intelligent task agent.\n"
            "1. Break down the task, produce an execution plan, and run it step by step.\n"
            "2. When essential information is missing, proactively ask the user (request_input); do not fabricate.\n"
            "3. Image attachments are provided as image content; analyze them directly, do not call read_file. "
            "Other attachments are provided as virtual paths (e.g. /uploads/example.txt); "
            "read them with local file tools, do not treat them as http resources or Windows paths.\n"
            "4. Request permission (request_permission) before high-risk tool operations.\n"
            "5. Always report back to the user when the whole task is finished.\n"
            "Always respond in the user's language."
        )
    agent_md = _load_agent_md(AGENT_NAME)
    if agent_md:
        return f"{base_prompt}\n\n# Agent Descriptions\n{agent_md}"
    return base_prompt

SYSTEM_PROMPT = get_system_prompt()


class PowerShellBackend(LocalShellBackend):
    """Windows-specific shell backend: execute commands go through PowerShell.

    The default LocalShellBackend uses subprocess.run(shell=True), which on
    Windows actually invokes cmd.exe - neither bash nor PowerShell - so model
    commands in either syntax fail before being retried. Here we explicitly
    call powershell.exe so the model can write PowerShell syntax directly
    (paired with the command cheat-sheet at the top of AGENTS.md).
    """

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            msg = f"timeout must be positive, got {effective_timeout}"
            raise ValueError(msg)

        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                check=False,
                shell=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=effective_timeout,
                env=self._env,
                cwd=str(self.cwd),
            )

            # same output assembly logic as LocalShellBackend.execute
            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                stderr_lines = result.stderr.strip().split("\n")
                output_parts.extend(f"[stderr] {line}" for line in stderr_lines)

            output = "\n".join(output_parts) if output_parts else "<no output>"

            truncated = False
            if len(output) > self._max_output_bytes:
                output = output[: self._max_output_bytes]
                output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
                truncated = True

            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"

            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=truncated,
            )

        except subprocess.TimeoutExpired:
            if timeout is not None:
                msg = f"Error: Command timed out after {effective_timeout} seconds (custom timeout). The command may be stuck or require more time."
            else:
                msg = f"Error: Command timed out after {effective_timeout} seconds. For long-running commands, re-run using the timeout parameter."
            return ExecuteResponse(
                output=msg,
                exit_code=124,  # Standard timeout exit code
                truncated=False,
            )
        except Exception as e:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
            )


def build_agent(
    context: AgentSessionContext, checkpointer=None
) -> CompiledStateGraph:
    """Agent factory: build a DeepAgent from the session context (each session has independent state and working directory).

    Uses a virtual filesystem backend: when the agent reads/writes files or executes commands,
    ACP session/request_permission is triggered and must be approved by the frontend user.
    """
    agent_root_dir = getattr(context, "cwd", None) or "."

    ephemeral_backend = StateBackend()
    backend = CompositeBackend(
        # LocalShellBackend = file operations (inherits FilesystemBackend) + execute (shell commands)
        # virtual_mode=True maps virtual paths such as /uploads/example.txt to files under root_dir.
        # Shell commands still run in root_dir and are unrestricted by this filesystem mapping.
        # inherit_env=True: inherit the parent process environment (PATH/USERPROFILE, etc.); otherwise the child process
        #   has an empty environment and cannot even find powershell/whoami.
        # Windows uses PowerShellBackend: default shell=True actually runs cmd.exe, so
        #   PowerShell syntax fails immediately; switching to explicit powershell.exe lets
        #   the model execute PowerShell syntax directly.
        default=(PowerShellBackend if sys.platform == "win32" else LocalShellBackend)(
            root_dir=agent_root_dir,
            # virtual_mode=False: absolute paths (C:\, D:\, user home) are
            # passed through as-is so the built-in read_file/write_file/glob/
            # grep tools can access global Windows locations. Relative paths
            # still resolve under root_dir, so /uploads/... attachments keep
            # working. execute is unaffected (shell was never sandboxed).
            virtual_mode=False,
            inherit_env=True,
        ),
        routes={
            "/memories/": ephemeral_backend,
            "/conversation_history/": ephemeral_backend,
        },
    )
    # Build subagents from agents.json "subagents" field
    subagents: list[SubAgent] = []
    for sa_cfg in get_agent_subagents_config(AGENT_NAME):
        sa_name = sa_cfg.get("name")
        if not sa_name:
            continue
        sa: SubAgent = {
            "name": sa_name,
            "description": sa_cfg.get("description", ""),
        }
        sa_tools = get_agent_mcp_tools(sa_name)
        if sa_tools:
            sa["tools"] = sa_tools
        if sa_cfg.get("skills"):
            sa["skills"] = sa_cfg["skills"]
        if sa_cfg.get("system_prompt"):
            sa["system_prompt"] = sa_cfg["system_prompt"]
        subagents.append(sa)

    agent = create_deep_agent(
        model=MODEL,
        # MCP tools configured for the selected agent (agents.json -> mcp_tools);
        # each tool is a langchain StructuredTool backed by a fastmcp stdio client.
        tools=get_agent_mcp_tools(AGENT_NAME),
        subagents=subagents or None,
        # Skills enabled for the selected agent (agents.json -> skills; the
        # default agent starts with every installed skill).
        skills=get_agent_skills(AGENT_NAME),
        # persistent checkpointer (AsyncSqliteSaver): session state saved to agent_state.sqlite,
        # paired with AgentServerACP(load_sessions=True) for session/load history continuation.
        checkpointer=checkpointer,
        backend=backend,
        # The sandbox is off (virtual_mode=False): built-in file tools operate
        # on the real Windows filesystem. Tell the model its actual working
        # directory so ls/read_file/glob resolve relative paths correctly
        # instead of guessing a drive root.
        system_prompt=(
            f"{SYSTEM_PROMPT}\n\n"
            f"# 当前工作目录\n"
            f"工作目录为：{str(Path(agent_root_dir).resolve())}\n"
            f"内置文件工具（ls/read_file/glob/grep 等）可直接访问 Windows 全局绝对路径，"
            f"相对路径基于上述工作目录解析；/uploads/ 下的附件同样可读。\n"
        ),
        # Trigger HITL interrupt before risky tool calls -> deepagents_acp converts it to ACP
        # session/request_permission -> frontend shows permission prompt (allow / deny / always allow)
        # Any tool names can be configured: ls/read_file/glob/grep/delete/task/MCP tool, etc.
        interrupt_on={
            "execute": False,     # shell commands (remember "always allow" by command type)
            # "write_file": True,  # write files
            # "edit_file": True,   # edit files
            "delete": True,      # delete files
        },
    )
    return agent


class CompactingAgentServer(AgentServerACP):
    """AgentServerACP with automatic context compaction before every prompt.

    When the checkpointed session messages (plus a conservative overhead)
    reach CONTENT_SIZE * CONTEXT_COMPACT_RATIO, the earliest messages are
    summarized and removed from the checkpoint, then the prompt proceeds with
    the compacted history. Compaction failures never break the prompt.
    """

    async def prompt(self, prompt, session_id, message_id=None, **kwargs):
        try:
            agent = self._checkpointed_agent(session_id)
            config = self._session_config(session_id)
            state = await agent.aget_state(config)
            values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
            messages = list((values or {}).get("messages", []) or [])
            if messages and should_compact(messages):
                logger.info(
                    "[compact] session=%s tokens=%s overhead=%s -> compacting",
                    session_id,
                    estimate_tokens(messages),
                    os.getenv("CONTEXT_COMPACT_OVERHEAD") or 6000,
                )
                result = await compact_messages(agent, config, messages)
                logger.info("[compact] session=%s result=%s", session_id, result)
        except Exception:  # noqa: BLE001 - compaction must never break the prompt
            logger.exception("[compact] failed; continuing without compaction")
        return await super().prompt(prompt, session_id=session_id, message_id=message_id, **kwargs)


def _drop_last_user_turn(messages: list[Any]) -> list[Any]:
    """Return messages with the last user turn (and everything after it) removed.

    Used by retry: the client re-sends the same user text, so keeping the
    original user message would duplicate it. Dropping the whole last turn
    leaves history at the previous assistant reply and the re-sent prompt
    rebuilds the turn from a clean slate.
    """
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, HumanMessage) or getattr(msg, "type", "") == "human":
            last_user_idx = i
            break
    if last_user_idx is None:
        return list(messages)
    return list(messages[:last_user_idx])


class RollbackAgentServer(CompactingAgentServer):
    """CompactingAgentServer that also rolls the session checkpoint back to
    its pre-prompt messages when a run is cancelled.

    LangGraph persists every node's messages to the checkpointer as it
    streams, so a cancelled run leaves partial assistant output / tool calls
    in the checkpoint. Without a rollback the next prompt resumes from that
    dirty state and the new answer is stitched onto the cancelled run's
    leftovers. On cancel we snapshot the messages right before the prompt and
    restore them (REMOVE_ALL_MESSAGES + replay) so the next prompt starts from
    the clean pre-prompt history.

    A prompt sent with `_meta.retry` (the frontend regenerate button re-sends
    the same user text) additionally rolls the checkpoint back by one turn
    before running, so the model re-answers on the previous assistant reply
    instead of on top of the old answer it is meant to replace.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pre_prompt_messages: dict[str, list[Any]] = {}
        self._cancel_rollback: set[str] = set()

    async def prompt(self, prompt, session_id, message_id=None, **kwargs):
        if kwargs.get("retry"):
            await self._rollback_last_turn(session_id)
        try:
            agent = self._checkpointed_agent(session_id)
            config = self._session_config(session_id)
            state = await agent.aget_state(config)
            values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
            self._pre_prompt_messages[session_id] = list((values or {}).get("messages", []) or [])
        except Exception:  # noqa: BLE001 - snapshot must never break the prompt
            self._pre_prompt_messages.pop(session_id, None)
        result = await super().prompt(prompt, session_id=session_id, message_id=message_id, **kwargs)
        if (
            result is not None
            and getattr(result, "stop_reason", None) == "cancelled"
            and session_id in self._cancel_rollback
        ):
            self._cancel_rollback.discard(session_id)
            await self._rollback_checkpoint(session_id)
        return result

    async def cancel(self, session_id, **kwargs):
        self._cancel_rollback.add(session_id)
        await super().cancel(session_id, **kwargs)

    async def _rollback_last_turn(self, session_id: str) -> None:
        try:
            agent = self._checkpointed_agent(session_id)
            config = self._session_config(session_id)
            state = await agent.aget_state(config)
            values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
            messages = list((values or {}).get("messages", []) or [])
            pre = _drop_last_user_turn(messages)
            await agent.aupdate_state(
                config,
                {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *pre]},
            )
            logger.info("[retry] session=%s rolled back last user turn (%d -> %d)", session_id, len(messages), len(pre))
        except Exception:  # noqa: BLE001 - retry rollback must never break the prompt
            logger.exception("[retry] rollback failed; continuing with existing history")

    async def _rollback_checkpoint(self, session_id: str) -> None:
        pre = self._pre_prompt_messages.pop(session_id, None)
        if pre is None:
            return
        try:
            agent = self._checkpointed_agent(session_id)
            config = self._session_config(session_id)
            await agent.aupdate_state(
                config,
                {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *pre]},
            )
            logger.info("[rollback] session=%s restored %d pre-prompt messages", session_id, len(pre))
        except Exception:  # noqa: BLE001 - rollback must never break the next prompt
            logger.exception("[rollback] failed for session=%s", session_id)


async def main() -> None:
    """Start the stdio ACP service; deepagents_acp handles ACP protocol encoding/decoding and event publishing."""
    conn = await aiosqlite.connect(DB_PATH)
    checkpointer = AsyncSqliteSaver(conn)
    acp_agent = RollbackAgentServer(
        # every session reuses the same persistent checkpointer; load_sessions=True makes
        # initialize advertise loadSession:true and implements session/load (replays history update events).
        agent=lambda ctx: build_agent(ctx, checkpointer),
        load_sessions=True,
    )
    try:
        await run_agent(acp_agent)
    finally:
        await close_mcp_clients()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

