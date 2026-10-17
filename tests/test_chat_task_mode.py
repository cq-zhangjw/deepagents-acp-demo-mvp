"""chat/task 模式分离：build_agent 裁剪 + mode 判定 验证脚本。

用法：python tests/test_chat_task_mode.py
验证：
- mode='chat'  → tools/skills/subagents 全为 None，系统提示为轻量聊天提示（不含工作目录/文件工具）
- mode='task' / 缺省 / 'auto' / 'default' → 完整工具链 + 工作目录提示（与现状一致）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acp_agent  # noqa: E402
from deepagents_acp.server import AgentSessionContext  # noqa: E402

# 避免构建子 agent 时真实启动 MCP 子进程 / 读取 agents.json
acp_agent.get_agent_subagents_config = lambda name: []
acp_agent.get_agent_mcp_tools = lambda name: []
acp_agent.get_agent_skills = lambda name: ["demo-skill"]

_failures: list[str] = []
_captured: dict = {}


def fake_create_deep_agent(**kwargs):
    _captured.update(kwargs)
    return "agent-stub"


acp_agent.create_deep_agent = fake_create_deep_agent


def build(mode):
    _captured.clear()
    acp_agent.build_agent(AgentSessionContext(cwd=".", mode=mode, model=None))
    return dict(_captured)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        _failures.append(name)
        print(f"  ✗ {name} {detail}")


print("== chat 模式裁剪 ==")
cap = build("chat")
check("tools=None", cap["tools"] is None)
check("subagents=None", cap["subagents"] is None)
check("skills=None", cap["skills"] is None)
prompt = cap["system_prompt"]
check("提示含「轻量聊天助手」", "轻量聊天助手" in prompt)
check("提示不含「当前工作目录」", "当前工作目录" not in prompt)
check("提示不含「内置文件工具」", "内置文件工具" not in prompt)
check("提示含「新建一个任务会话」", "新建一个任务会话" in prompt)

print("== task / 缺省 / auto / default 完整能力 ==")
for mode in ("task", None, "auto", "default"):
    cap = build(mode)
    check(f"mode={mode!r} tools 非空", cap["tools"] is not None)
    check(f"mode={mode!r} skills 非空", cap["skills"] is not None)
    prompt = cap["system_prompt"]
    check(f"mode={mode!r} 提示含工作目录", "当前工作目录" in prompt)
    check(f"mode={mode!r} 提示含内置文件工具", "内置文件工具" in prompt)

print()
if _failures:
    print(f"FAILED: {len(_failures)} 项未通过 → {_failures}")
    sys.exit(1)
print("全部通过 ✓")
