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
MCP_DIR = DE_ROOT / "mcp_servers"
AGENTS_FILE = DE_ROOT / "agents.json"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# directory creation is guaranteed on first module import
for _d in (DE_ROOT, SKILLS_DIR, MCP_DIR):
    _d.mkdir(parents=True, exist_ok=True)
if not AGENTS_FILE.exists():
    _write_agents({})

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
    return {"ok": True}


# ---------- tools (MCP servers) ----------

def _server_entry(server_dir: Path) -> dict:
    cfg = _read_json(server_dir / "server.json", {})
    return {
        "name": server_dir.name,
        "description": cfg.get("description", ""),
        "command": cfg.get("command", ""),
        "enabled": not _is_disabled(server_dir),
        "path": str(server_dir.relative_to(BASE_DIR)).replace("\\", "/"),
        "updated_at": _mtime(server_dir / "server.json"),
    }


@router.get("/tools")
def list_tools():
    """MCP server list: scan .deepagents/mcp_servers/*/server.json."""
    items = []
    if MCP_DIR.is_dir():
        for d in sorted(MCP_DIR.iterdir()):
            if d.is_dir() and (d / "server.json").exists():
                items.append(_server_entry(d))
    items.sort(key=lambda x: (not x["enabled"], x["name"]))
    return items


@router.post("/tools/{name}/toggle")
def toggle_tool(name: str):
    """Toggle an MCP server."""
    name = _ensure_name(name)
    d = MCP_DIR / name
    if not (d / "server.json").exists():
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    flag = d / ".disabled"
    if flag.exists():
        flag.unlink()
        enabled = True
    else:
        flag.touch()
        enabled = False
    return {"name": name, "enabled": enabled}


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
    """Probe a specific MCP server and return available tool names or the error."""
    name = _ensure_name(name)
    d = MCP_DIR / name
    cfg = _read_json(d / "server.json", {})
    if not cfg:
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
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
    """Delete an MCP config directory (irreversible; frontend double-confirms)."""
    name = _ensure_name(name)
    d = MCP_DIR / name
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    try:
        shutil.rmtree(d)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"failed to delete tool: {exc}") from exc
    return {"ok": True}


# ---------- agents (global agents.json) ----------

def _agent_entry(name: str, data: dict) -> dict:
    return {
        "name": name,
        "description": (data.get("description") or ""),
        "enabled": data.get("enabled", True),
        "model": data.get("model"),
        "system_prompt": data.get("system_prompt"),
        "skills": data.get("skills") or [],
        "tools": data.get("tools") or [],
    }


@router.get("/agents")
def list_agents():
    """Agent list: read the global agents.json."""
    agents = _read_agents()
    items = [_agent_entry(name, data) for name, data in agents.items()]
    items.sort(key=lambda x: (not x["enabled"], x["name"]))
    return items


@router.get("/agents/{name}")
def get_agent(name: str):
    name = _ensure_name(name)
    agents = _read_agents()
    if name not in agents:
        raise HTTPException(status_code=404, detail=f"agent not found: {name}")
    return _agent_entry(name, agents[name])


async def _validate_agent(name: str, data: dict) -> dict:
    """Pre-check: linked skills/tools exist and are enabled."""
    problems = []
    for skill in data.get("skills") or []:
        if not (SKILLS_DIR / skill / "SKILL.md").exists():
            problems.append(f"skill not found: {skill}")
        elif _is_disabled(SKILLS_DIR / skill):
            problems.append(f"skill disabled: {skill}")
    for tool in data.get("tools") or []:
        if not (MCP_DIR / tool / "server.json").exists():
            problems.append(f"tool not found: {tool}")
        elif _is_disabled(MCP_DIR / tool):
            problems.append(f"tool disabled: {tool}")
    return {"name": name, "ok": not problems, "problems": problems}


@router.get("/agents/{name}/validate")
async def validate_agent(name: str):
    name = _ensure_name(name)
    agents = _read_agents()
    if name not in agents:
        raise HTTPException(status_code=404, detail=f"agent not found: {name}")
    return await _validate_agent(name, agents[name])


@router.post("/agents")
async def create_agent(payload: dict):
    """Create an agent: write to the global agents.json."""
    name = _ensure_name(payload.get("name", ""))
    async with _agents_lock:
        agents = _read_agents()
        if name in agents:
            raise HTTPException(status_code=409, detail=f"agent already exists: {name}")
        agents[name] = _normalize_agent_def(payload)
        _write_agents(agents)
    return _agent_entry(name, agents[name])


@router.put("/agents/{name}")
async def update_agent(name: str, payload: dict):
    """Edit an agent: overwrite the AgentDef for this key."""
    name = _ensure_name(name)
    async with _agents_lock:
        agents = _read_agents()
        if name not in agents:
            raise HTTPException(status_code=404, detail=f"agent not found: {name}")
        agents[name] = _normalize_agent_def(payload)
        _write_agents(agents)
    return _agent_entry(name, agents[name])


@router.delete("/agents/{name}")
async def delete_agent(name: str):
    """Delete an agent: remove this key from agents.json."""
    name = _ensure_name(name)
    async with _agents_lock:
        agents = _read_agents()
        if name not in agents:
            raise HTTPException(status_code=404, detail=f"agent not found: {name}")
        del agents[name]
        _write_agents(agents)
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
    skills = [s for s in (payload.get("skills") or []) if isinstance(s, str) and _valid_name(s)]
    tools = [t for t in (payload.get("tools") or []) if isinstance(t, str) and _valid_name(t)]
    if skills:
        def_data["skills"] = skills
    if tools:
        def_data["tools"] = tools
    return def_data
