"""MCP tool loader for DeepAgents.

Reads `.deepagents/agents/agents.json` (the per-agent tool configuration) and
turns each enabled MCP tool into a langchain StructuredTool backed by a shared
fastmcp stdio Client per server.

Conventions:
- Agent config path: <project>/.deepagents/agents/agents.json
- MCP server home: <project>/.deepagents/tools/mcp_servers/<server>
- A server directory must contain a manifest.json (tool metadata) and exactly
  one `.py` entry file (preferring server.py / main.py when present). The entry
  is launched as `python <entry>` via the fastmcp stdio client.
- Connections are created lazily on first tool call and shared process-wide;
  call `close_mcp_clients()` once at shutdown.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from fastmcp import Client
from fastmcp.client.transports.stdio import PythonStdioTransport
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, create_model

BASE_DIR = Path(__file__).resolve().parent.parent
AGENTS_FILE = BASE_DIR / ".deepagents" / "agents" / "agents.json"
MCP_SERVERS_DIR = BASE_DIR / ".deepagents" / "tools" / "mcp_servers"

_servers: dict[str, "ServerHolder"] = {}
_tools_cache: dict[str, list[BaseTool]] = {}
_agents_cache: dict[str, dict[str, Any]] | None = None
_CALL_TIMEOUT_SECONDS = float(os.getenv("MCP_CALL_TIMEOUT", "60"))


def load_agents_config() -> dict[str, dict[str, Any]]:
    """Load agents.json once and cache it (agents change rarely at runtime)."""
    global _agents_cache
    if _agents_cache is not None:
        return _agents_cache
    try:
        import json

        with open(AGENTS_FILE, "r", encoding="utf-8") as f:
            _agents_cache = json.load(f)
    except Exception:  # noqa: BLE001 - missing/malformed config degrades to empty
        _agents_cache = {}
    return _agents_cache


def _server_entry(server: str) -> Path:
    """Resolve the stdio entry script for an MCP server directory.

    Prefer server.py / main.py, otherwise require exactly one .py entry file.
    """
    server_dir = MCP_SERVERS_DIR / server
    if not server_dir.is_dir():
        raise FileNotFoundError(f"MCP server directory not found: {server_dir}")
    for preferred in ("server.py", "main.py"):
        candidate = server_dir / preferred
        if candidate.is_file():
            return candidate
    py_files = sorted(server_dir.glob("*.py"))
    if len(py_files) != 1:
        raise FileNotFoundError(
            f"MCP server '{server}' must contain exactly one .py entry file "
            f"(got {len(py_files)}); add server.py/main.py if ambiguous"
        )
    return py_files[0]


class ServerHolder:
    """Lazily connected fastmcp stdio client for one MCP server."""

    def __init__(self, server: str) -> None:
        self.server = server
        self._client: Client | None = None
        # fastmcp infers a PythonStdioTransport from the entry script Path and
        # launches it with the current interpreter (the project venv).
        self._entry = _server_entry(server)

    async def ensure(self) -> Client:
        if self._client is None or not self._client.is_connected:
            # stdio subprocess env: inherit the parent environment but force
            # UTF-8 so non-ASCII payloads never trip the pipe codec.
            client = Client(
                PythonStdioTransport(
                    script_path=self._entry,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                ),
                name=self.server,
            )
            # fastmcp 4.x connects inside its async context manager; enter it
            # manually so the connection outlives a single call.
            await client.__aenter__()
            self._client = client
        return self._client

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        client = await self.ensure()
        # Bound the server call: a hung MCP stdio subprocess must not leave the
        # agent prompt pending forever (which would keep the frontend stop
        # button active after the user sees no progress).
        result = await client.call_tool(tool_name, arguments, timeout=_CALL_TIMEOUT_SECONDS)
        data = getattr(result, "data", None)
        if data is not None:
            return str(data)
        content = getattr(result, "content", None)
        if content:
            parts = []
            for block in content:
                text = getattr(block, "text", None)
                if text is not None:
                    parts.append(str(text))
            if parts:
                return "\n".join(parts)
        return str(result)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass
            self._client = None


def _holder(server: str) -> ServerHolder:
    if server not in _servers:
        _servers[server] = ServerHolder(server)
    return _servers[server]


def _field_type(prop: dict[str, Any]) -> type:
    """Map a JSON schema type to a pydantic-compatible Python type."""
    prop_type = prop.get("type")
    if prop_type == "string":
        return str
    if prop_type == "integer":
        return int
    if prop_type in ("number",):
        return float
    if prop_type == "boolean":
        return bool
    return Any


def _build_args_model(name: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    """Convert a tool inputSchema into a pydantic model for StructuredTool."""
    properties = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])
    fields: dict[str, tuple[type, Any]] = {}
    for field_name, prop in properties.items():
        field_type = _field_type(prop)
        if field_name in required:
            fields[field_name] = (field_type, ...)
        else:
            fields[field_name] = (Optional[field_type], None)
    return create_model(f"{name}_args", __base__=BaseModel, **fields)


def make_tool(holder: ServerHolder, meta: dict[str, Any]) -> StructuredTool:
    """Wrap one manifest tool definition as an async langchain tool.

    The tool is named `mcp_<server>_<tool>` so MCP tools are visually distinct
    from the built-in deepagents tools (read_file, glob, execute, ...). The
    underlying server call still uses the original manifest tool name.
    """
    tool_name = f"mcp_{holder.server}_{meta['name']}"

    async def _run(**kwargs: Any) -> str:
        return await holder.call(meta["name"], kwargs)

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool_name,
        description=meta.get("description") or f"Call {meta['name']} on the MCP server",
        args_schema=_build_args_model(tool_name, meta.get("inputSchema") or {}),
    )


def get_agent_mcp_tools(agent_name: str) -> list[BaseTool]:
    """Return the enabled MCP tools configured for the given agent."""
    if agent_name in _tools_cache:
        return _tools_cache[agent_name]
    config = load_agents_config().get(agent_name) or {}
    entries = (config.get("tools") or {}).get("mcp_tools") or []
    tools: list[BaseTool] = []
    for entry in entries:
        server = entry.get("server")
        if not server:
            continue
        try:
            holder = _holder(server)
        except Exception:  # noqa: BLE001 - a broken server entry must not break the agent
            continue
        for tool_meta in entry.get("tools") or []:
            tools.append(make_tool(holder, tool_meta))
    _tools_cache[agent_name] = tools
    return tools


def get_agent_subagents_config(agent_name: str) -> list[dict[str, Any]]:
    """Return subagent config entries for the given agent from agents.json."""
    config = load_agents_config().get(agent_name) or {}
    return config.get("subagents") or []


async def close_mcp_clients() -> None:
    """Close every shared MCP client (call once at shutdown)."""
    for holder in _servers.values():
        await holder.close()
    _servers.clear()
    _tools_cache.clear()
