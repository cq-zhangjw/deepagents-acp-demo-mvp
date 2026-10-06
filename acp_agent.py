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
import os
import subprocess
import sys

import aiosqlite
from acp import run_agent
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend, StateBackend
from deepagents.backends.protocol import ExecuteResponse
from deepagents_acp.server import AgentServerACP, AgentSessionContext
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph

from utils.model_util import MODEL

# 会话状态持久化文件：checkpoint 落盘（sqlite），跨进程/跨网关重启保留，
# 前端可用 session/load 加载历史会话并继续对话。

DB_ROOT = os.getenv("DB_ROOT") if os.getenv("DB_ROOT") else "./db"
os.makedirs(DB_ROOT, exist_ok=True)
DB_PATH = os.path.join(DB_ROOT, "agent_state.sqlite")


# SYSTEM_PROMPT = (
#     "你是一个智能任务Agent。\n"
#     "1. 先拆解任务，生成执行计划，分步执行。\n"
#     "2. 缺少必要信息时主动向用户发起提问（request_input），不要编造信息。\n"
#     "3. 图片附件会作为图片内容直接提供，请直接分析图片，不要调用 read_file。"
#     "其他上传附件会以虚拟路径（如 /uploads/example.txt）提供；"
#     "请使用本地文件工具读取该路径，不要将其当作 http 资源或 Windows 文件路径访问。\n"
#     "4. 需要执行高风险工具操作时发起权限请求（request_permission）。\n" \
#     "5. 全体任务执行完毕后，你始终需要向用户进行报告。"
# )



def get_system_prompt() -> str:
    """Return the system prompt for the agent."""
    if os.path.exists("./AGENTS.md"):
        with open("./AGENTS.md", "r", encoding="utf-8") as f:
            return f.read()
    else:
        return (
            "你是一个智能任务Agent。\n"
            "1. 先拆解任务，生成执行计划，分步执行。\n"
            "2. 缺少必要信息时主动向用户发起提问（request_input），不要编造信息。\n"
            "3. 图片附件会作为图片内容直接提供，请直接分析图片，不要调用 read_file。"
            "其他上传附件会以虚拟路径（如 /uploads/example.txt）提供；"
            "请使用本地文件工具读取该路径，不要将其当作 http 资源或 Windows 文件路径访问。\n"
            "4. 需要执行高风险工具操作时发起权限请求（request_permission）。\n" \
            "5. 全体任务执行完毕后，你始终需要向用户进行报告。"
        )

SYSTEM_PROMPT = get_system_prompt()


class PowerShellBackend(LocalShellBackend):
    """Windows 专用 shell 后端：execute 命令统一交给 PowerShell 执行。

    默认 LocalShellBackend 用 subprocess.run(shell=True)，在 Windows 上实际走的是
    cmd.exe——既不是 bash 也不是 PowerShell，模型无论写 Linux 命令还是 PowerShell
    命令都会先报错再试错。这里改为显式调用 powershell.exe，模型按 PowerShell
    语法执行即可（配合 AGENTS.md 顶部的命令对照表）。
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

            # 与 LocalShellBackend.execute 相同的输出组装逻辑
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
        # Windows 上用 PowerShellBackend：默认 shell=True 实际走 cmd.exe，模型写 PowerShell
        #   语法会直接报错；改为显式 powershell.exe 后模型按 PowerShell 语法执行即可。
        default=(PowerShellBackend if sys.platform == "win32" else LocalShellBackend)(
            root_dir=agent_root_dir,
            virtual_mode=True,
            inherit_env=True,
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
        # 持久化 checkpointer（AsyncSqliteSaver）：会话状态落盘 agent_state.sqlite，
        # 配合 AgentServerACP(load_sessions=True) 支持 session/load 加载历史并继续对话。
        checkpointer=checkpointer,
        backend=backend,
        system_prompt=SYSTEM_PROMPT,
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


async def main() -> None:
    """Start the stdio ACP service; deepagents_acp handles ACP protocol encoding/decoding and event publishing."""
    conn = await aiosqlite.connect(DB_PATH)
    checkpointer = AsyncSqliteSaver(conn)
    acp_agent = AgentServerACP(
        # 每个 session 复用同一个持久化 checkpointer；load_sessions=True 使 initialize
        # 广告 loadSession:true，并实现 session/load（重放历史 update 事件）。
        agent=lambda ctx: build_agent(ctx, checkpointer),
        load_sessions=True,
    )
    await run_agent(acp_agent)


if __name__ == "__main__":
    asyncio.run(main())

