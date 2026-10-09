"""DeepAgents management panel API (sub-router, prefix /api/manage).

Manages three resource types under the local `.deepagents/` directory:
  - skills/      skills (directory + SKILL.md, Anthropic Agent Skills format)
  - mcp_servers/ MCP server configs (directory + server.json, stdio launch spec)
  - agents.json  agent definitions (single global file, object map { <name>: AgentDef })

The panel only reads/writes config files (CRUD / enable-disable); how acp_agent.py
assembles model/skills/MCP tools from an agent is implemented by the caller,
this module does not touch assembly logic.

Safety conventions:
  - name whitelist `^[a-z0-9][a-z0-9-]{0,63}$` prevents path traversal
  - deletes are limited to whitelisted .deepagents directories/keys
  - agents.json writes: in-process write lock + same-dir .bak backup + atomic replace
"""

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/manage", tags=["manage"])

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # project root
DE_ROOT = BASE_DIR / ".deepagents"
SKILLS_DIR = DE_ROOT / "skills"
MCP_DIR = DE_ROOT / "tools" / "mcp_servers"
AGENTS_DIR = DE_ROOT / "agents"
AGENTS_FILE = AGENTS_DIR / "agents.json"
AGENT_MD_SUFFIX = ".agent.md"

# allows underscores too (MCP server dirs like calc_server are common)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# directory creation is guaranteed on first module import
for _d in (DE_ROOT, SKILLS_DIR, MCP_DIR, AGENTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_layout() -> None:
    """One-time migration: .deepagents/mcp_servers -> .deepagents/tools/mcp_servers."""
    legacy = DE_ROOT / "mcp_servers"
    if legacy.is_dir() and not MCP_DIR.exists():
        try:
            MCP_DIR.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(MCP_DIR))
            logger.info("migrated %s -> %s", legacy, MCP_DIR)
        except OSError as exc:
            logger.error("migrate mcp_servers failed: %s", exc)


_migrate_legacy_layout()

_agents_lock = asyncio.Lock()


# ---------- common helpers ----------

def _valid_name(name: str) -> bool:
    return bool(name and _NAME_RE.match(name))


def _ensure_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _valid_name(name):
        raise HTTPException(status_code=400, detail=f"invalid name: {name!r} (use [a-z0-9-], max 64)")
    return name


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    tmp.replace(path)


def _write_agents(data: dict) -> None:
    _write_json_atomic(AGENTS_FILE, data)


def _read_agents() -> dict:
    return _read_json(AGENTS_FILE, {})


# ---------- built-in tools ----------

BUILTIN_TOOLS = [
    {"name": "ls", "description": "List directory contents"},
    {"name": "read_file", "description": "Read a file's content"},
    {"name": "write_file", "description": "Write a file"},
    {"name": "edit_file", "description": "Edit a file in place"},
    {"name": "glob", "description": "Find files by name pattern"},
    {"name": "grep", "description": "Search file contents by regex"},
    {"name": "execute", "description": "Run shell commands"},
    {"name": "task", "description": "Call a subagent"},
]


@router.get("/builtin-tools")
def list_builtin_tools():
    """List the tools every deep agent gets by default (read-only reference)."""
    return BUILTIN_TOOLS


BUILTIN_TOOL_NAMES = [t["name"] for t in BUILTIN_TOOLS]


def _default_agent_tools() -> dict:
    """AgentDef.tools default: all builtin tools enabled, no MCP tools.

    inner_tools: None = all builtin tools enabled; a list = the explicitly
    enabled builtin tool names.
    mcp_tools: list of manifest-style entries (serverInfo + selected tools[]).
    """
    return {"inner_tools": None, "mcp_tools": []}


def _norm_inner_tools(value) -> list | None:
    """Normalize inner_tools: None (all enabled) or a validated list of names."""
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    names = [v for v in value if isinstance(v, str) and v in BUILTIN_TOOL_NAMES]
    if not names:
        return None
    # dedupe, keep order
    return list(dict.fromkeys(names))


def _norm_agent_tools(value) -> dict:
    """Normalize an AgentDef.tools field.

    Accepts the new dict form {inner_tools, mcp_tools}, the legacy list form
    (server names -> full manifest entries), or nothing (defaults).
    """
    if isinstance(value, dict):
        return {
            "inner_tools": _norm_inner_tools(value.get("inner_tools")),
            "mcp_tools": _norm_mcp_tools(value.get("mcp_tools")),
        }
    if isinstance(value, list):
        # legacy: ["filesystem-mcp", ...] -> full manifest entries, inner all on
        entries = []
        for s in value:
            if isinstance(s, str) and _valid_name(s) and (MCP_DIR / s).is_dir():
                entries.append(_agent_mcp_entry(s, [t.get("name") for t in _manifest_tools(MCP_DIR / s)]))
        return {"inner_tools": None, "mcp_tools": entries}
    return _default_agent_tools()


def _norm_mcp_tools(value) -> list[dict]:
    """Normalize mcp_tools: manifest-style entries whose tools[] are selected."""
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        server = _mcp_entry_server(entry)
        if not server or server in seen:
            continue
        tools = entry.get("tools")
        if not isinstance(tools, list):
            continue
        sel = [t for t in tools if isinstance(t, dict) and t.get("name")]
        if not sel:
            continue
        seen.add(server)
        out.append({
            "server": server,
            "serverInfo": entry.get("serverInfo"),
            "tools": sel,
        })
    return out


def _agent_mcp_entry(server: str, tools: list[dict]) -> dict:
    """Build a manifest-style mcp_tools entry for a server."""
    server_dir = MCP_DIR / server
    manifest = _read_json(server_dir / "manifest.json", {})
    return {
        "serverInfo": manifest.get("serverInfo") or {"name": server, "version": ""},
        "tools": tools,
    }


def _agent_md_template(name: str, data: dict) -> str:
    """Default .agent.md body for a new agent."""
    description = (data.get("description") or "").strip()
    front = "---\nname: {0}\n".format(name)
    if description:
        front += "description: {0}\n".format(description)
    return front + "---\n\n# {0}\n\nDefine the agent behavior here (system prompt / instructions).\n".format(name)


def _agent_md_path(name: str) -> Path:
    return AGENTS_DIR / (name + AGENT_MD_SUFFIX)


def _migrate_legacy_agents() -> None:
    """One-time migration: old `.deepagents/agents.json` -> `agents/*.agent.md` + `agents/agents.json`."""
    legacy = DE_ROOT / "agents.json"
    if not legacy.exists() or AGENTS_FILE.exists():
        return
    data = _read_json(legacy, {})
    _write_json_atomic(AGENTS_FILE, data)
    for name, defn in data.items():
        md = _agent_md_path(name)
        if not md.exists():
            md.write_text(_agent_md_template(name, defn), encoding="utf-8")
    try:
        legacy.unlink()
    except OSError:
        pass


_migrate_legacy_agents()

if not AGENTS_FILE.exists():
    _write_agents({})


def _frontmatter(md: str) -> dict:
    """Parse the YAML frontmatter of SKILL.md (name/description etc., lenient parse)."""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end > 0:
            meta = {}
            for line in md[3:end].splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip().lower()] = val.strip()
            return meta
    return {}


def _is_disabled(dir_path: Path) -> bool:
    return (dir_path / ".disabled").exists()


def _skill_entry(skill_dir: Path) -> dict:
    md_path = skill_dir / "SKILL.md"
    name = skill_dir.name
    description = ""
    try:
        meta = _frontmatter(md_path.read_text(encoding="utf-8"))
        description = meta.get("description", "")
        fname = meta.get("name", "")
        if fname and fname != name:
            name = fname
    except OSError:
        pass
    return {
        "name": name,
        "description": description,
        "enabled": not _is_disabled(skill_dir),
        "path": str(skill_dir.relative_to(BASE_DIR)).replace("\\", "/"),
        "absolute_path": str(skill_dir.resolve()),
        "updated_at": _mtime(md_path),
    }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------- tools.json (aggregated enabled-tool list) ----------

def _manifest_tools(server_dir: Path) -> list[dict]:
    """Read manifest.json tools of a server; returns the raw tool entries."""
    try:
        manifest = _read_json(server_dir / "manifest.json", {})
        tools = manifest.get("tools") or []
        return tools if isinstance(tools, list) else []
    except Exception:  # noqa: BLE001
        return []


# directories/files skipped by the @ file picker
_SKIP_TOP = {".git", ".venv", "node_modules", "__pycache__", ".idea", ".vscode"}


@router.get("/files")
def list_files(q: str = ""):
    """List top-level entries of the project root (for the @ file picker).

    Returns name / absolute path / type, filtered by name prefix (q, case-insensitive).
    Only one level is listed; dot-prefixed files like .env are kept, but common
    heavyweight/vendor dirs (git, venv, node_modules, __pycache__) are skipped.
    """
    q = (q or "").strip().lower()
    entries = []
    try:
        for p in sorted(BASE_DIR.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if p.name in _SKIP_TOP:
                continue
            if q and not p.name.lower().startswith(q):
                continue
            entries.append({
                "name": p.name,
                "path": str(p.resolve()),
                "type": "dir" if p.is_dir() else "file",
            })
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to list files: {exc}") from exc
    return entries


# ---------- skills ----------

@router.get("/skills")
def list_skills():
    """Skill list: scan .deepagents/skills/*/SKILL.md."""
    items = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                items.append(_skill_entry(d))
    items.sort(key=lambda x: (not x["enabled"], x["name"]))
    return items


@router.get("/skills/{name}")
def get_skill(name: str):
    """Fetch a single skill's SKILL.md body for editing."""
    name = _ensure_name(name)
    md = SKILLS_DIR / name / "SKILL.md"
    if not md.exists():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    try:
        return {"name": name, "content": md.read_text(encoding="utf-8")}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read skill: {exc}") from exc


@router.post("/skills/{name}/toggle")
def toggle_skill(name: str):
    """Toggle a skill (create/delete the .disabled marker file)."""
    name = _ensure_name(name)
    d = SKILLS_DIR / name
    if not (d / "SKILL.md").exists():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    flag = d / ".disabled"
    if flag.exists():
        flag.unlink()
        enabled = True
    else:
        flag.touch()
        enabled = False
        # disabling drops the skill from every agent's association list
        _remove_agent_ref("skill", name)
    return {"name": name, "enabled": enabled}


@router.post("/skills")
def create_skill(payload: dict):
    """Create a skill: name / description / content (SKILL.md body, may include frontmatter)."""
    name = _ensure_name(payload.get("name", ""))
    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")
    d = SKILLS_DIR / name
    if (d / "SKILL.md").exists():
        raise HTTPException(status_code=409, detail=f"skill already exists: {name}")
    d.mkdir(parents=True, exist_ok=True)
    meta = _frontmatter(content)
    description = (payload.get("description") or "").strip()
    if not meta.get("name"):
        # auto-generate frontmatter if the body has none
        content = f"---\nname: {name}\ndescription: {description}\n---\n\n{content}"
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return _skill_entry(d)


@router.put("/skills/{name}")
def update_skill(name: str, payload: dict):
    """Edit a skill: overwrite the whole file content (incl. description)."""
    name = _ensure_name(name)
    d = SKILLS_DIR / name
    md_path = d / "SKILL.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")
    md_path.write_text(content, encoding="utf-8")
    return _skill_entry(d)


@router.delete("/skills/{name}")
def delete_skill(name: str):
    """Delete a skill directory (irreversible; frontend double-confirms)."""
    name = _ensure_name(name)
    d = SKILLS_DIR / name
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    try:
        shutil.rmtree(d)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"failed to delete skill: {exc}") from exc
    _remove_agent_ref("skill", name)
    return {"ok": True}


# ---------- tools (MCP servers, per-agent enabled set in agents.json) ----------

def _agent_tools_def(agent: str) -> dict:
    """Get (and lazily create) the AgentDef.tools structure of an agent."""
    agents = _read_agents()
    defn = agents.setdefault(agent, {})
    tools = defn.get("tools")
    if not isinstance(tools, dict):
        tools = _default_agent_tools()
        defn["tools"] = tools
    if "inner_tools" not in tools or "mcp_tools" not in tools:
        tools = _default_agent_tools()
        defn["tools"] = tools
    return defn["tools"], agents


def _agent_mcp_entry(server: str, tool_names: list[str]) -> dict:
    """Manifest-style mcp_tools entry for a server, tools[] filtered to selection.

    `server` = directory name (the stable identity used for matching);
    `serverInfo` keeps the manifest display info as-is.
    """
    server_dir = MCP_DIR / server
    manifest = _read_json(server_dir / "manifest.json", {})
    all_tools = manifest.get("tools") or []
    sel = [t for t in all_tools if t.get("name") in set(tool_names)]
    return {
        "server": server,
        "serverInfo": manifest.get("serverInfo") or {"name": server, "version": ""},
        "tools": sel,
    }


def _mcp_entry_server(entry: dict) -> str:
    """Identity of an mcp_tools entry: directory name (legacy entries fall back to serverInfo.name)."""
    s = entry.get("server")
    if isinstance(s, str) and s:
        return s
    info = entry.get("serverInfo") or {}
    return str(info.get("name") or "").strip()


def _server_entry(server_dir: Path, tools_def: dict) -> dict:
    """Group entry for one MCP server, enable state driven by the agent's tools.

    server.enabled  = an entry for this server exists in agent.mcp_tools;
    tool.enabled    = the tool name is inside that entry's tools[].
    """
    cfg = _read_json(server_dir / "server.json", {})
    manifest = _read_json(server_dir / "manifest.json", {})
    server_info = manifest.get("serverInfo") or {}
    mcp_tools = tools_def.get("mcp_tools") or []
    entry = next((e for e in mcp_tools if _mcp_entry_server(e) == server_dir.name), None)
    enabled_names = {t.get("name") for t in (entry or {}).get("tools") or []} if entry else set()
    tools = []
    for t in manifest.get("tools") or []:
        tname = t.get("name", "")
        tools.append({
            "name": tname,
            "description": (t.get("description") or "").strip(),
            "enabled": tname in enabled_names,
        })
    description = (
        (cfg.get("description") or "").strip()
        or (server_info.get("name") or "").strip()
    )
    return {
        "name": server_dir.name,
        "description": description,
        "command": cfg.get("command", ""),
        "has_server_json": (server_dir / "server.json").exists(),
        "enabled": entry is not None,
        "tools": tools,
        "path": str(server_dir.relative_to(BASE_DIR)).replace("\\", "/"),
        "updated_at": _mtime(server_dir / "manifest.json") or _mtime(server_dir / "server.json"),
    }


@router.get("/tools")
def list_tools(agent: str = "default"):
    """MCP server groups for an agent: scan .deepagents/tools/mcp_servers/*/.

    Enable state comes from the agent's tools.mcp_tools in agents.json.
    """
    agent = _ensure_name(agent)
    tools_def, _ = _agent_tools_def(agent)
    items = []
    if MCP_DIR.is_dir():
        for d in sorted(MCP_DIR.iterdir()):
            if d.is_dir() and ((d / "manifest.json").exists() or (d / "server.json").exists()):
                items.append(_server_entry(d, tools_def))
    items.sort(key=lambda x: (not x["enabled"], x["name"]))
    return items


@router.post("/tools/{name}/toggle")
def toggle_tool(name: str, agent: str = "default"):
    """Toggle an MCP server for one agent (group level).

    Enabling adds a manifest-style entry (all tools selected) to the agent's
    tools.mcp_tools; disabling removes the entry. Only the agent's tools
    field in agents.json is touched.
    """
    name = _ensure_name(name)
    agent = _ensure_name(agent)
    d = MCP_DIR / name
    if not d.is_dir() or not ((d / "manifest.json").exists() or (d / "server.json").exists()):
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    tools_def, agents = _agent_tools_def(agent)
    mcp_tools = tools_def.get("mcp_tools") or []
    entry = next((e for e in mcp_tools if _mcp_entry_server(e) == name), None)
    if entry is not None:
        tools_def["mcp_tools"] = [e for e in mcp_tools if _mcp_entry_server(e) != name]
        enabled = False
    else:
        tools_def["mcp_tools"] = mcp_tools + [_agent_mcp_entry(name, [t.get("name") for t in _manifest_tools(d)])]
        enabled = True
    _write_agents(agents)
    return {"name": name, "enabled": enabled, "agent": agent}


@router.post("/tools/{server}/{tool}/toggle")
def toggle_tool_item(server: str, tool: str, agent: str = "default"):
    """Toggle a single tool inside a server for one agent.

    The tool is added/removed in the agent's mcp_tools entry; if no entry
    exists yet, enabling a tool creates the server entry with that tool.
    """
    server = _ensure_name(server)
    agent = _ensure_name(agent)
    if not tool:
        raise HTTPException(status_code=400, detail="tool name must not be empty")
    d = MCP_DIR / server
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"tool not found: {server}")
    manifest = _read_json(d / "manifest.json", {})
    names = {t.get("name") for t in manifest.get("tools") or []}
    if tool not in names:
        raise HTTPException(status_code=404, detail=f"tool not found: {server}/{tool}")
    tools_def, agents = _agent_tools_def(agent)
    mcp_tools = tools_def.get("mcp_tools") or []
    entry = next((e for e in mcp_tools if _mcp_entry_server(e) == server), None)
    if entry is not None:
        sel = [t.get("name") for t in entry.get("tools") or []]
        if tool in sel:
            sel = [x for x in sel if x != tool]
            enabled = False
        else:
            sel.append(tool)
            enabled = True
        if sel:
            entry["tools"] = [t for t in manifest.get("tools") or [] if t.get("name") in set(sel)]
            tools_def["mcp_tools"] = [e if e is not entry else entry for e in mcp_tools]
        else:
            tools_def["mcp_tools"] = [e for e in mcp_tools if e is not entry]
    else:
        tools_def["mcp_tools"] = mcp_tools + [_agent_mcp_entry(server, [tool])]
        enabled = True
    _write_agents(agents)
    return {"server": server, "tool": tool, "enabled": enabled, "agent": agent}


@router.post("/agents/{agent}/inner-tools/{tool}/toggle")
def toggle_inner_tool(agent: str, tool: str):
    """Toggle one builtin tool for an agent (agents.json tools.inner_tools).

    inner_tools: None = all builtin tools enabled (default); toggling a tool
    off materializes the full list minus that tool; toggling back on restores
    None when the list equals the full builtin set.
    """
    agent = _ensure_name(agent)
    if tool not in BUILTIN_TOOL_NAMES:
        raise HTTPException(status_code=404, detail=f"inner tool not found: {tool}")
    tools_def, agents = _agent_tools_def(agent)
    inner = _norm_inner_tools(tools_def.get("inner_tools"))
    if inner is None:
        # currently all enabled -> turn this one off
        inner = [n for n in BUILTIN_TOOL_NAMES if n != tool]
        enabled = False
    elif tool in inner:
        inner = [n for n in inner if n != tool]
        enabled = False
    else:
        inner = inner + [tool]
        enabled = True
    tools_def["inner_tools"] = None if set(inner) == set(BUILTIN_TOOL_NAMES) else inner
    _write_agents(agents)
    return {"agent": agent, "tool": tool, "enabled": enabled}


def _resolve_env(env: dict) -> dict:
    """Resolve ${VAR} placeholders in env from the process environment; keep unknown ones as-is."""
    resolved = {}
    for key, value in (env or {}).items():
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            var = value[2:-1]
            resolved[key] = os.getenv(var, value)
        else:
            resolved[key] = value
    return resolved


async def _probe_mcp(cfg: dict) -> tuple[list[str], str | None]:
    """Probe an MCP server: stdio launch -> initialize -> list_tools -> close.

    Returns:
        (tools, error): empty tools means failure, error holds the reason.
    """
    command = (cfg.get("command") or "").strip()
    if not command:
        return [], "command is required"
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=command,
            args=cfg.get("args") or [],
            env=_resolve_env(cfg.get("env") or {}),
            cwd=cfg.get("cwd") or None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools], None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


@router.post("/tools")
async def create_tool(payload: dict):
    """Create an MCP config: name / command / args / cwd / env / description, then auto-probe."""
    name = _ensure_name(payload.get("name", ""))
    command = (payload.get("command") or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command must not be empty")
    d = MCP_DIR / name
    if (d / "server.json").exists():
        raise HTTPException(status_code=409, detail=f"tool already exists: {name}")
    cfg = {
        "name": name,
        "description": (payload.get("description") or "").strip(),
        "command": command,
        "args": [str(a) for a in (payload.get("args") or [])],
        "cwd": (payload.get("cwd") or "").strip() or None,
        "env": payload.get("env") or {},
    }
    d.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(d / "server.json", cfg)
    tools, error = await _probe_mcp(cfg)
    return {**_server_entry(d), "probe": {"tools": tools, "error": error}}


@router.post("/tools/{name}/test")
async def test_tool(name: str):
    """Return the tool list of a server.

    With a server.json the server is probed (stdio launch -> list_tools);
    without one (pure-function server) the tools are read from manifest.json.
    """
    name = _ensure_name(name)
    d = MCP_DIR / name
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    cfg = _read_json(d / "server.json", {})
    if not cfg:
        return {"name": name, "tools": [t.get("name", "") for t in _manifest_tools(d)], "error": None}
    tools, error = await _probe_mcp(cfg)
    return {"name": name, "tools": tools, "error": error}


@router.put("/tools/{name}")
async def update_tool(name: str, payload: dict):
    """Edit an MCP config and re-probe."""
    name = _ensure_name(name)
    d = MCP_DIR / name
    cfg_path = d / "server.json"
    if not cfg_path.exists():
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    old = _read_json(cfg_path, {})
    command = (payload.get("command") or "").strip() or old.get("command", "")
    if not command:
        raise HTTPException(status_code=400, detail="command must not be empty")
    cfg = {
        "name": name,
        "description": (payload.get("description") if payload.get("description") is not None else old.get("description", "")).strip(),
        "command": command,
        "args": [str(a) for a in (payload.get("args") if payload.get("args") is not None else old.get("args", []))],
        "cwd": (payload.get("cwd") or old.get("cwd") or "").strip() or None,
        "env": payload.get("env") if payload.get("env") is not None else old.get("env", {}),
    }
    _write_json_atomic(cfg_path, cfg)
    tools, error = await _probe_mcp(cfg)
    return {**_server_entry(d), "probe": {"tools": tools, "error": error}}


@router.delete("/tools/{name}")
def delete_tool(name: str):
    """Delete an MCP server directory (irreversible; frontend double-confirms)."""
    name = _ensure_name(name)
    d = MCP_DIR / name
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    try:
        shutil.rmtree(d)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"failed to delete tool: {exc}") from exc
    _remove_agent_ref("tool", name)
    return {"ok": True}


# ---------- agents (agents/*.agent.md + agents/agents.json) ----------

def _remove_agent_ref(kind: str, name: str) -> None:
    """Remove a skill/tool reference from every agent (disable/delete cleanup).

    skills: drop the name from the enabled skills list.
    tools:  drop the whole mcp_tools entry whose serverInfo.name == name.
    Re-enabling does NOT re-add it (user picks it again).
    """
    agents = _read_agents()
    changed = False
    for defn in agents.values():
        if kind == "skill":
            items = defn.get("skills") or []
            if name in items:
                defn["skills"] = [x for x in items if x != name]
                changed = True
        elif kind == "tool":
            tools = defn.get("tools")
            if isinstance(tools, dict):
                mcp = tools.get("mcp_tools") or []
                kept = [e for e in mcp if _mcp_entry_server(e) != name]
                if len(kept) != len(mcp):
                    tools["mcp_tools"] = kept
                    changed = True
    if changed:
        _write_agents(agents)


def _agent_md_body(name: str) -> str:
    md = _agent_md_path(name)
    if md.exists():
        try:
            return md.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def _agent_entry(name: str, data: dict) -> dict:
    return {
        "name": name,
        "description": (data.get("description") or ""),
        "enabled": data.get("enabled", True),
        "model": data.get("model"),
        "system_prompt": data.get("system_prompt"),
        "skills": data.get("skills") or [],
        "tools": _norm_agent_tools(data.get("tools")),
        "file": name + AGENT_MD_SUFFIX,
        "body": _agent_md_body(name),
    }


def _scan_agent_files() -> list[str]:
    """Agent names = `agents/*.agent.md` files (merged with agents.json keys)."""
    names = set()
    if AGENTS_DIR.is_dir():
        for p in AGENTS_DIR.glob("*" + AGENT_MD_SUFFIX):
            names.add(p.name[: -len(AGENT_MD_SUFFIX)])
    names.update(_read_agents().keys())
    return sorted(names)


@router.get("/agents")
def list_agents():
    """Agent list: scan `agents/*.agent.md`, merge association data from `agents/agents.json`."""
    agents = _read_agents()
    items = [_agent_entry(name, agents.get(name) or {}) for name in _scan_agent_files()]
    items.sort(key=lambda x: (not x["enabled"], x["name"]))
    return items


@router.get("/agents/{name}")
def get_agent(name: str):
    name = _ensure_name(name)
    agents = _read_agents()
    if _agent_md_path(name).exists() or name in agents:
        return _agent_entry(name, agents.get(name) or {})
    raise HTTPException(status_code=404, detail=f"agent not found: {name}")


async def _validate_agent(name: str, data: dict) -> dict:
    """Pre-check: linked skills/tools exist (associations are enabled-only by design)."""
    problems = []
    for skill in data.get("skills") or []:
        if not (SKILLS_DIR / skill / "SKILL.md").exists():
            problems.append(f"skill not found: {skill}")
    for tool in data.get("tools") or []:
        if not (MCP_DIR / tool / "server.json").exists():
            problems.append(f"tool not found: {tool}")
    return {"name": name, "ok": not problems, "problems": problems}


@router.get("/agents/{name}/validate")
async def validate_agent(name: str):
    name = _ensure_name(name)
    agents = _read_agents()
    if _agent_md_path(name).exists() or name in agents:
        return await _validate_agent(name, agents.get(name) or {})
    raise HTTPException(status_code=404, detail=f"agent not found: {name}")


@router.post("/agents")
async def create_agent(payload: dict):
    """Create an agent: write `<name>.agent.md` + association entry in `agents/agents.json`."""
    name = _ensure_name(payload.get("name", ""))
    body = (payload.get("body") or "").strip()
    async with _agents_lock:
        agents = _read_agents()
        if _agent_md_path(name).exists() or name in agents:
            raise HTTPException(status_code=409, detail=f"agent already exists: {name}")
        defn = _normalize_agent_def(payload)
        agents[name] = defn
        _write_agents(agents)
        if not body:
            body = _agent_md_template(name, defn)
        _agent_md_path(name).write_text(body, encoding="utf-8")
    return _agent_entry(name, agents[name])

@router.put("/agents/{name}")
async def update_agent(name: str, payload: dict):
    """Edit an agent: overwrite `.agent.md` (if body given) and the agents.json entry."""
    name = _ensure_name(name)
    async with _agents_lock:
        agents = _read_agents()
        if not (_agent_md_path(name).exists() or name in agents):
            raise HTTPException(status_code=404, detail=f"agent not found: {name}")
        defn = _normalize_agent_def(payload, agents.get(name, {}).get("tools") or {})
        agents[name] = defn
        _write_agents(agents)
        body = (payload.get("body") or "").strip()
        if body:
            _agent_md_path(name).write_text(body, encoding="utf-8")
        elif not _agent_md_path(name).exists():
            _agent_md_path(name).write_text(_agent_md_template(name, defn), encoding="utf-8")
    return _agent_entry(name, agents[name])


@router.delete("/agents/{name}")
async def delete_agent(name: str):
    """Delete an agent: remove `*.agent.md` and the agents.json key."""
    name = _ensure_name(name)
    async with _agents_lock:
        agents = _read_agents()
        md = _agent_md_path(name)
        if not (md.exists() or name in agents):
            raise HTTPException(status_code=404, detail=f"agent not found: {name}")
        agents.pop(name, None)
        _write_agents(agents)
        md.unlink(missing_ok=True)
    return {"ok": True}


def _normalize_agent_def(payload: dict, existing_tools: dict | None = None) -> dict:
    """Whitelist fields: keep only the known AgentDef keys."""
    def_data = {
        "description": (payload.get("description") or "").strip(),
        "enabled": bool(payload.get("enabled", True)),
    }
    model = payload.get("model")
    if isinstance(model, dict) and model:
        allowed = {k: v for k, v in model.items() if k in ("provider", "model_name", "base_url", "api_key", "temperature", "max_tokens", "top_p")}
        if allowed:
            def_data["model"] = allowed
    sp = payload.get("system_prompt")
    if isinstance(sp, str) and sp.strip():
        def_data["system_prompt"] = sp.strip()
    skills = [s for s in (payload.get("skills") or []) if isinstance(s, str) and _valid_name(s)
              and not _is_disabled(SKILLS_DIR / s)]
    if skills:
        def_data["skills"] = skills
    tools = _norm_agent_tools(payload.get("tools"))
    # the agent form sends a server-name list; keep the existing per-agent
    # inner_tools state instead of resetting it to "all enabled"
    if isinstance(payload.get("tools"), list) and existing_tools:
        tools["inner_tools"] = _norm_inner_tools(existing_tools.get("inner_tools"))
    if not (tools["inner_tools"] is None and not tools["mcp_tools"]):
        def_data["tools"] = tools
    return def_data
