"""Skills configured for an agent (agents.json -> AgentDef.skills).

The skills array is the single source of enablement:
  - null / missing field = the full installed set (default-agent behavior);
  - an explicit array (including []) = the exact enabled set.
The legacy global `.disabled` marker file is still honored as a hard-disable
(skills whose directory carries it are never assembled).

deepagents' `skills` parameter is a list of *source directories*; each source
is scanned for subdirectories that contain a SKILL.md (one skill each). To
respect per-agent enablement we materialize a per-agent "enabled set" directory
(.deepagents/skills/.enabled/<agent>/<skill>/SKILL.md, hard-linked to the real
SKILL.md so edits stay in sync) and return that directory as the single source.
"""

import os
import shutil
from pathlib import Path

from utils.mcp_loader import load_agents_config

BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BASE_DIR / ".deepagents" / "skills"
ENABLED_ROOT = SKILLS_DIR / ".enabled"


def list_skill_names() -> list[str]:
    """Directory names of installed skills (stable identity, sorted)."""
    names = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                names.append(d.name)
    return names


def _selected_names(agent_name: str) -> list[str]:
    all_names = list_skill_names()
    # hard-disable: a .disabled marker removes the skill from every agent
    all_names = [n for n in all_names if not (SKILLS_DIR / n / ".disabled").exists()]
    name = (agent_name or os.getenv("AGENT_NAME", "default")).strip() or "default"
    cfg = load_agents_config().get(name) or {}
    skills = cfg.get("skills")
    if not isinstance(skills, list):
        return all_names
    wanted = {s for s in skills if isinstance(s, str) and s in all_names}
    return [n for n in all_names if n in wanted]


def _materialize_enabled_set(agent_name: str, selected: list[str]) -> Path:
    """Rebuild .deepagents/skills/.enabled/<agent>/<skill>/SKILL.md (hard links).

    Returns the per-agent enabled-set directory.
    """
    d = ENABLED_ROOT / agent_name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    for n in selected:
        src = SKILLS_DIR / n / "SKILL.md"
        dst = d / n
        dst.mkdir()
        try:
            os.link(src, dst / "SKILL.md")
        except OSError:
            shutil.copy2(src, dst / "SKILL.md")
    return d


def get_agent_skills(agent_name: str | None = None) -> list[str]:
    """Skill source path enabled for an agent (deepagents source directory).

    Builds a per-agent enabled-set directory (hard-linked SKILL.md files) and
    returns it as a single source. The backend runs with virtual_mode=True, so
    the path must be POSIX and relative to the project root (the backend's
    root_dir) — a Windows absolute path would be re-resolved under root and
    miss.
    """
    name = (agent_name or os.getenv("AGENT_NAME", "default")).strip() or "default"
    selected = _selected_names(name)
    if not selected:
        return []
    d = _materialize_enabled_set(name, selected)
    return [f"{SKILLS_DIR.relative_to(BASE_DIR).as_posix()}/.enabled/{name}"]
