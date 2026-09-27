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

from acp import run_agent
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend, StateBackend
from deepagents_acp.server import AgentServerACP, AgentSessionContext
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from utils.model_util import MODEL

SYSTEM_PROMPT = (
    "你是一个智能任务Agent。\n"
    "1. 先拆解任务，生成执行计划，分步执行。\n"
    "2. 缺少必要信息时主动向用户发起提问（request_input），不要编造信息。\n"
    "3. 读取外部 http 资源（图片/文件）前，请求用户权限。\n"
    "4. 需要执行高风险工具操作时发起权限请求（request_permission）。\n" \
    "5. 全体任务执行完毕后，你始终需要向用户进行报告。"
)


def build_agent(context: AgentSessionContext) -> CompiledStateGraph:
    """Agent factory: build a DeepAgent from the session context (each session has independent state and working directory).

    Uses a virtual filesystem backend: when the agent reads or writes files/resources, ACP
    session/request_permission is triggered and must be approved by the frontend user.
    """
    agent_root_dir = getattr(context, "cwd", None) or "."

    ephemeral_backend = StateBackend()
    backend = CompositeBackend(
        # LocalShellBackend = file operations (inherits FilesystemBackend) + execute (shell commands)
        # virtual_mode=False: no working-directory restriction — absolute paths are accessed as-is (entire disk),
        #   relative paths are resolved from cwd; shell commands also run in cwd and are unrestricted by path.
        # inherit_env=True: inherit the parent process environment (PATH/USERPROFILE, etc.); otherwise the child process
        #   has an empty environment and cannot even find powershell/whoami.
        default=LocalShellBackend(
            root_dir=agent_root_dir,
            virtual_mode=False,
            inherit_env=True
        ),
        routes={
            "/memories/": ephemeral_backend,
            "/conversation_history/": ephemeral_backend,
        },
    )

    agent = create_deep_agent(
        model=MODEL,
        # tools=mcp_loader._global_mcp_tools,  # use cached tools
        tools=None,
        checkpointer=MemorySaver(),
        backend=backend,
        system_prompt=SYSTEM_PROMPT,
        # Trigger HITL interrupt before risky tool calls -> deepagents_acp converts it to ACP
        # session/request_permission -> frontend shows permission prompt (allow / deny / always allow)
        # Any tool names can be configured: ls/read_file/glob/grep/delete/task/MCP tool, etc.
        interrupt_on={
            "execute": True,     # shell commands (remember "always allow" by command type)
            # "write_file": True,  # write files
            # "edit_file": True,   # edit files
            "delete": True,      # delete files
        },
    )
    return agent


async def main() -> None:
    """Start the stdio ACP service; deepagents_acp handles ACP protocol encoding/decoding and event publishing."""
    acp_agent = AgentServerACP(agent=build_agent)
    await run_agent(acp_agent)


if __name__ == "__main__":
    asyncio.run(main())

