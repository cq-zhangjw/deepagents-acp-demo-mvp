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
TOOLS_FILE = BASE_DIR / "tools.json"  # aggregated list of enabled tools (root level)
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

# root tools.json holds the aggregated enabled-tool list; ensure it exists (default: [])
if not TOOLS_FILE.exists():
    TOOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOOLS_FILE, "w", encoding="utf-8") as _f:
        json.dump([], _f, ensure_ascii=False, indent=2)

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

def _read_tools() -> list[dict]:
    """Read root tools.json (list of enabled tools). Default: empty list."""
    data = _read_json(TOOLS_FILE, [])
    return data if isinstance(data, list) else []


def _write_tools(items: list[dict]) -> None:
    _write_json_atomic(TOOLS_FILE, items)


def _manifest_tools(server_dir: Path) -> list[dict]:
    """Read manifest.json tools of a server; returns the raw tool entries."""
    try:
        manifest = _read_json(server_dir / "manifest.json", {})
        tools = manifest.get("tools") or []
        return tools if isinstance(tools, list) else []
    except Exception:  # noqa: BLE001
        return []


def _add_tools_to_registry(server: str, server_dir: Path) -> None:
    """Append every tool of a server (from manifest.json) to tools.json."""
    items = _read_tools()
    existing = {(_t.get("server"), _t.get("name")) for _t in items}
    added = False
    for t in _manifest_tools(server_dir):
        key = (server, t.get("name", ""))
        if key in existing:
            continue
        items.append({"server": server, **t})
        added = True
    if added:
        _write_tools(items)


def _remove_tools_from_registry(server: str, tool: str | None = None) -> None:
    """Drop a server's tools (or a single tool) from tools.json."""
    items = _read_tools()
    if tool is None:
        items = [_t for _t in items if _t.get("server") != server]
    else:
        items = [_t for _t in items if not (_t.get("server") == server and _t.get("name") == tool)]
    _write_tools(items)


def _tool_enabled(server: str, tool: str) -> bool:
    return any(_t.get("server") == server and _t.get("name") == tool for _t in _read_tools())


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


# ---------- tools (MCP servers, grouped under .deepagents/tools/mcp_servers) ----------

def _server_entry(server_dir: Path) -> dict:
    """Group entry for one MCP server: server-level info + per-tool list.

    Tools come from manifest.json (each server root carries one); a tool is
    enabled iff its (server, name) entry exists in the root tools.json.
    server.json remains an optional launch config (command/env), absent for
    pure-function servers.
    """
    cfg = _read_json(server_dir / "server.json", {})
    manifest = _read_json(server_dir / "manifest.json", {})
    server_info = manifest.get("serverInfo") or {}
    tools = []
    for t in _manifest_tools(server_dir):
        tname = t.get("name", "")
        tools.append({
            "name": tname,
            "description": (t.get("description") or "").strip(),
            "enabled": _tool_enabled(server_dir.name, tname),
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
        "enabled": not _is_disabled(server_dir),
        "tools": tools,
        "path": str(server_dir.relative_to(BASE_DIR)).replace("\\", "/"),
        "updated_at": _mtime(server_dir / "manifest.json") or _mtime(server_dir / "server.json"),
    }


@router.get("/tools")
def list_tools():
    """MCP server groups: scan .deepagents/tools/mcp_servers/*/ (manifest.json)."""
    items = []
    if MCP_DIR.is_dir():
        for d in sorted(MCP_DIR.iterdir()):
            if d.is_dir() and ((d / "manifest.json").exists() or (d / "server.json").exists()):
                items.append(_server_entry(d))
    items.sort(key=lambda x: (not x["enabled"], x["name"]))
    return items


@router.post("/tools/{name}/toggle")
def toggle_tool(name: str):
    """Toggle an MCP server (group level).

    Disabling drops every tool of the server from tools.json and from all
    agent association lists; enabling re-reads manifest.json and registers
    all its tools in tools.json (no auto re-add to agents).
    """
    name = _ensure_name(name)
    d = MCP_DIR / name
    if not d.is_dir() or not ((d / "manifest.json").exists() or (d / "server.json").exists()):
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    flag = d / ".disabled"
    if flag.exists():
        flag.unlink()
        enabled = True
        _add_tools_to_registry(name, d)
    else:
        flag.touch()
        enabled = False
        _remove_tools_from_registry(name)
        # disabling drops the MCP server from every agent's association list
        _remove_agent_ref("tool", name)
    return {"name": name, "enabled": enabled}


@router.post("/tools/{server}/{tool}/toggle")
def toggle_tool_item(server: str, tool: str):
    """Toggle a single tool inside an MCP server group (tools.json entry)."""
    server = _ensure_name(server)
    if not tool:
        raise HTTPException(status_code=400, detail="tool name must not be empty")
    d = MCP_DIR / server
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"tool not found: {server}")
    if _is_disabled(d):
        raise HTTPException(status_code=409, detail=f"server disabled: {server}")
    names = {t.get("name") for t in _manifest_tools(d)}
    if tool not in names:
        raise HTTPException(status_code=404, detail=f"tool not found: {server}/{tool}")
    if _tool_enabled(server, tool):
        _remove_tools_from_registry(server, tool)
        enabled = False
    else:
        manifest = _read_json(d / "manifest.json", {})
        entry = next((t for t in (manifest.get("tools") or []) if t.get("name") == tool), {})
        items = _read_tools()
        items.append({"server": server, **entry})
        _write_tools(items)
        enabled = True
    return {"server": server, "tool": tool, "enabled": enabled}


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
    _remove_tools_from_registry(name)
    _remove_agent_ref("tool", name)
    return {"ok": True}


# ---------- agents (agents/*.agent.md + agents/agents.json) ----------

def _remove_agent_ref(kind: str, name: str) -> None:
    """Remove a skill/tool reference from every agent's list (disable/delete cleanup).

    agents.json `skills`/`tools` hold only *enabled* associations; when a skill or
    MCP server is disabled or deleted, drop it from all agents so no stale
    reference survives. Re-enabling does NOT re-add it (user picks it again).
    """
    key = "skills" if kind == "skill" else "tools"
    agents = _read_agents()
    changed = False
    for defn in agents.values():
        items = defn.get(key) or []
        if name in items:
            defn[key] = [x for x in items if x != name]
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
        "tools": data.get("tools") or [],
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
        defn = _normalize_agent_def(payload)
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


def _normalize_agent_def(payload: dict) -> dict:
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
    tools = [t for t in (payload.get("tools") or []) if isinstance(t, str) and _valid_name(t)
             and not _is_disabled(MCP_DIR / t)]
    if skills:
        def_data["skills"] = skills
    if tools:
        def_data["tools"] = tools
    return def_data
