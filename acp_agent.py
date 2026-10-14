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
from langchain_core.messages import HumanMessage, RemoveMessage, convert_to_messages
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
from utils.context_compactor import compact_messages, compact_messages_list, estimate_tokens, should_compact
from utils.session_context import (  # noqa: E402  (imported after env/DB setup, matches module layout)
    get_context as sc_get_context,
    init_from_checkpoint as sc_init_from_checkpoint,
    rollback_last_turn as sc_rollback_last_turn,
    save_context as sc_save_context,
)
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


def get_system_prompt(agent_name: str | None = None) -> str:
    """Return the system prompt for the agent, appending agent md content when available.

    `agent_name` selects `.deepagents/agents/<name>.agent.md` (per-task agent in the
    timer engine); when omitted it falls back to the startup `AGENT_NAME` env var.
    """
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
    name = (agent_name or os.getenv("AGENT_NAME", "") or "default").strip() or "default"
    agent_md = _load_agent_md(name)
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
    context: AgentSessionContext, checkpointer=None, agent_name: str | None = None
) -> CompiledStateGraph:
    """Agent factory: build a DeepAgent from the session context (each session has independent state and working directory).

    `agent_name` overrides the AGENT_NAME env var (used by the in-process timer
    engine to run each scheduled task with the agent it was created for).

    Uses a virtual filesystem backend: when the agent reads/writes files or executes commands,
    ACP session/request_permission is triggered and must be approved by the frontend user.
    """
    agent_root_dir = getattr(context, "cwd", None) or "."
    agent_name = (agent_name or os.getenv("AGENT_NAME", "") or "default").strip() or "default"

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
    for sa_cfg in get_agent_subagents_config(agent_name):
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
        tools=get_agent_mcp_tools(agent_name),
        subagents=subagents or None,
        # Skills enabled for the selected agent (agents.json -> skills; the
        # default agent starts with every installed skill).
        skills=get_agent_skills(agent_name),
        # persistent checkpointer (AsyncSqliteSaver): session state saved to agent_state.sqlite,
        # paired with AgentServerACP(load_sessions=True) for session/load history continuation.
        checkpointer=checkpointer,
        backend=backend,
        # The sandbox is off (virtual_mode=False): built-in file tools operate
        # on the real Windows filesystem. Tell the model its actual working
        # directory so ls/read_file/glob resolve relative paths correctly
        # instead of guessing a drive root.
        system_prompt=(
            f"{get_system_prompt(agent_name)}\n\n"
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


class RollbackAgentServer(CompactingAgentServer):
    """Session-context-table authoritative ACP server with cancel/retry safety.

    The conversation history actually fed to the model lives in the MooFile
    context table (`db/session_content.bson`, see utils/session_context.py),
    NOT in the langgraph checkpoint. Per prompt:

      1. read the table (first run for a session: seed it once from the
         checkpoint);
      2. compact against the table content (pure function; result written back
         to the table);
      3. re-seed the checkpoint from the table (REMOVE_ALL_MESSAGES + replay)
         so langgraph executes on exactly the table's history — the checkpoint
         stays a runtime store (breakpoints / crash recovery) but no longer
         decides what the model sees next;
      4. run the standard ACP prompt (AgentServerACP directly; the legacy
         checkpoint-side compaction in CompactingAgentServer is bypassed);
      5. write the run result back to the table from the checkpoint.

    Cancel: the table stays at the pre-prompt snapshot (this turn is abandoned)
    and the checkpoint is rolled back too, so the next prompt starts clean.
    Retry (`_meta.retry`): the last user turn is dropped from the table before
    running, so the re-sent user text does not duplicate.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pre_prompt_messages: dict[str, list[Any]] = {}
        self._cancel_rollback: set[str] = set()
        # Per-session agent override (used by the timer engine: each scheduled
        # task may run with the agent it was created for, unlike the WS flow
        # where AGENT_NAME is fixed per subprocess).
        self._session_agents: dict[str, str] = {}

    def _reset_agent(self, session_id: str) -> None:
        """Reset the agent instance, re-creating it from the factory if applicable.

        Honors the per-session agent override (`_session_agents`) so the
        in-process server can switch agents between scheduled task sessions.
        """
        cwd = self._session_cwds.get(session_id)
        if cwd is not None:
            self._cwd = cwd
        if isinstance(self._agent_factory, CompiledStateGraph):
            self._agent = self._agent_factory
        else:
            mode = self._session_modes.get(
                session_id,
                self._modes.current_mode_id if self._modes is not None else "auto",
            )
            model = self._session_models.get(session_id) if self._models is not None else None
            context = AgentSessionContext(cwd=self._cwd, mode=mode, model=model)
            agent_name = self._session_agents.get(session_id)
            if agent_name:
                self._agent = self._agent_factory(context, agent_name=agent_name)
            else:
                self._agent = self._agent_factory(context)
        self._agent_session_id = session_id

    async def prompt(self, prompt, session_id, message_id=None, **kwargs):
        # Retry: drop the last user turn from the context table (the client
        # re-sends the same user text, so the old copy must go).
        if kwargs.get("retry"):
            try:
                if sc_rollback_last_turn(session_id):
                    logger.info("[retry] session=%s rolled back last turn in context table", session_id)
            except Exception:  # noqa: BLE001 - retry rollback must never break the prompt
                logger.exception("[retry] table rollback failed; continuing")

        # ---- 1) context table is the single source of truth ----
        context_messages = sc_get_context(session_id)
        if context_messages is None:
            # First run for this session: seed the table once from the checkpoint.
            try:
                agent = self._checkpointed_agent(session_id)
                config = self._session_config(session_id)
                state = await agent.aget_state(config)
                values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
                ckpt_msgs = list((values or {}).get("messages", []) or [])
                context_messages = sc_init_from_checkpoint(session_id, ckpt_msgs)
            except Exception:  # noqa: BLE001 - seeding must never break the prompt
                logger.exception("[session_context] init from checkpoint failed; starting empty")
                context_messages = []

        # ---- 2) compaction acts on the table content ----
        try:
            objs = convert_to_messages(context_messages) if context_messages else []
            if objs and should_compact(objs):
                logger.info(
                    "[compact] session=%s tokens=%s overhead=%s -> compacting (table)",
                    session_id,
                    estimate_tokens(objs),
                    os.getenv("CONTEXT_COMPACT_OVERHEAD") or 6000,
                )
                compacted, result = await compact_messages_list(objs)
                sc_save_context(session_id, compacted)
                context_messages = [m.to_json() for m in compacted]
                logger.info("[compact] session=%s result=%s", session_id, result)
        except Exception:  # noqa: BLE001 - compaction must never break the prompt
            logger.exception("[compact] failed; continuing without compaction")

        # ---- 3) snapshot = table content before this prompt (cancel baseline) ----
        self._pre_prompt_messages[session_id] = context_messages

        # ---- 4) re-seed the checkpoint from the table ----
        try:
            agent = self._checkpointed_agent(session_id)
            config = self._session_config(session_id)
            objs = convert_to_messages(context_messages) if context_messages else []
            await agent.aupdate_state(
                config,
                {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *objs]},
                as_node="__start__",
            )
        except Exception:  # noqa: BLE001 - re-seed must never break the prompt
            logger.exception("[session_context] checkpoint re-seed failed; continuing")

        # Run the standard ACP prompt. AgentServerACP is called directly so the
        # legacy checkpoint-side compaction (CompactingAgentServer.prompt) does
        # not double-compact against the table.
        result = await AgentServerACP.prompt(
            self, prompt, session_id=session_id, message_id=message_id, **kwargs
        )
        if (
            result is not None
            and getattr(result, "stop_reason", None) == "cancelled"
            and session_id in self._cancel_rollback
        ):
            self._cancel_rollback.discard(session_id)
            # Table stays at the pre-prompt snapshot (this turn is abandoned).
            pre = self._pre_prompt_messages.get(session_id)
            if pre is not None:
                try:
                    sc_save_context(session_id, pre)
                except Exception:  # noqa: BLE001 - restore must never break the response
                    logger.exception("[session_context] cancel snapshot restore failed")
            await self._rollback_checkpoint(session_id)
            return result

        # ---- 5) write back the run result ----
        # The checkpoint now holds history + this turn; copy it back to the table.
        try:
            agent = self._checkpointed_agent(session_id)
            config = self._session_config(session_id)
            state = await agent.aget_state(config)
            values = state.values if hasattr(state, "values") else (state if isinstance(state, dict) else {})
            final_messages = list((values or {}).get("messages", []) or [])
            sc_save_context(session_id, final_messages)
        except Exception:  # noqa: BLE001 - write-back must never break the response
            logger.exception("[session_context] write-back failed")
        return result

    async def cancel(self, session_id, **kwargs):
        self._cancel_rollback.add(session_id)
        await super().cancel(session_id, **kwargs)

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

