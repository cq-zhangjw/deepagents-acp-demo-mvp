"""Skills configured for an agent (agents.json -> AgentDef.skills).

The skills array is the single source of enablement: the default agent starts
with every installed skill (missing / empty / non-array values normalize to the
full installed set, mirroring tools.inner_tools). acp_agent.py consumes
get_agent_skills() when assembling create_deep_agent(skills=...).
"""

import os
from pathlib import Path

from utils.mcp_loader import load_agents_config

BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BASE_DIR / ".deepagents" / "skills"


def list_skill_names() -> list[str]:
    """Directory names of installed skills (stable identity, sorted)."""
    names = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                names.append(d.name)
    return names


def get_agent_skills(agent_name: str | None = None) -> list[str]:
    """Skills enabled for an agent: the agents.json skills array.

    None / missing / empty values mean the full installed set (default agent
    behavior); unknown skill names are dropped from the result.
    """
    all_names = list_skill_names()
    if not all_names:
        return []
    name = (agent_name or os.getenv("AGENT_NAME", "default")).strip() or "default"
    cfg = load_agents_config().get(name) or {}
    skills = cfg.get("skills")
    if not isinstance(skills, list):
        return all_names
    selected = {s for s in skills if isinstance(s, str) and s in all_names}
    return [n for n in all_names if n in selected] if selected else all_names
