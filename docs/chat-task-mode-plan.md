# 聊天/任务会话模式分离 —— 实施方案

> 状态：**方案存档（暂不实施，后期执行）**
> 适用版本：当前 main（`acp_agent.py::build_agent` + `api/history.py` MooFile 会话表 + `ChatPage.vue`）
> 目标：区分「简单聊天」与「任务执行」——聊天会话不关联 agent/skills/MCP 工具/文件工具，省 token、减少工具干扰，同时保持现有任务能力完全不变。

---

## 1. 行为定义

| | 聊天模式（chat） | 任务模式（task） |
| --- | --- | --- |
| MCP 工具 | 不加载 | 按所选 agent 加载 |
| Skills | 不加载 | 按所选 agent 加载（default 为全部） |
| 子 agent（subagents） | 无 | 按 agents.json 加载 |
| 系统提示 | 轻量聊天提示（见 §3.2） | 现有完整提示（agent + 工作目录 + 文件工具说明） |
| 对话 / 上下文压缩 / 附件读取 | 正常 | 正常 |
| 绑定 agent | 固定轻量 default，不出现在 agent 选择中 | 可自由选择 agent |
| 定时任务 | **不允许**（无 agent 可执行） | 允许 |

**模式不可切换（用户确认）**：同一会话的模式一经创建即锁定。用户需要另一种模式时，**直接新建对应模式的会话**（新会话、空上下文）。不提供会话内切换/重建 agent 的入口，也不需要 ws 重连逻辑。

---

## 2. 数据模型

会话记录（`api/history.py` 的 MooFile `db_history` 表，宽松 dict 结构）增加字段：

```jsonc
{
  "id": "...",
  "title": "...",
  "updatedAt": 1234567890,
  "mode": "chat" | "task",   // 新增；缺省视为 task（兼容已有会话）
  "...": "..."
}
```

- `put_history` / `list_history` 天然透传该字段，无建表迁移；
- 已有会话无 `mode` 字段 → 后端按 `task` 处理（行为完全不变）。

---

## 3. 后端改动（`acp_agent.py`）

### 3.1 mode 来源（现成通道）

`acp_agent.py:390` 已有：

```python
context = AgentSessionContext(cwd=self._cwd, mode=mode, model=model)
```

`mode` 由前端建 ws 时传入（ACP 协议 session mode）——无需新增协议。前端 `?agent=X&mode=chat` 或 new_session 消息携带。

### 3.2 `build_agent()` 按 mode 裁剪（214-306 行）

```python
def build_agent(context, checkpointer=None, agent_name=None):
    ...
    mode = getattr(context, "mode", None) or "task"   # 缺省 task
    chat = mode == "chat"
    agent = create_deep_agent(
        model=MODEL,
        tools=None if chat else get_agent_mcp_tools(agent_name),   # chat: 无 MCP
        subagents=None if chat else (subagents or None),           # chat: 无子 agent
        skills=None if chat else get_agent_skills(agent_name),     # chat: 无 skills
        system_prompt=(
            CHAT_SYSTEM_PROMPT if chat else
            f"{get_system_prompt(agent_name)}\n\n# 当前工作目录\n..."
        ),
        ...
    )
```

**CHAT_SYSTEM_PROMPT**（新增常量，中文，与现有提示风格一致）：

```text
你是一个轻量聊天助手。
- 只回答问题与日常对话，不执行任何本地文件操作或 shell 命令。
- 不调用文件、代码或系统工具。
- 如果用户需要操作本地文件、运行代码或执行任务，请明确提示：
  「当前是聊天模式，请新建一个任务会话后再执行。」
```

### 3.3 边界

- 定时任务引擎（`timers_engine.py`）创建任务时强制 `mode="task"`（chat 会话不进入定时任务入口，前端层面拦截 + 后端兜底校验）；
- `api/context.py` 的 `_compact/_usage` 构建 agent 时沿用传入 mode（chat 会话压缩照常工作）。

---

## 4. 前端改动（`ChatPage.vue`）

1. **新建会话入口**：提供「新建聊天会话」「新建任务会话」两个入口（或一个入口内选择模式），新建时 `mode` 随会话创建写入；
2. **模式标识**：会话卡片显示「聊天 / 任务」小徽标；消息栏顶部显示当前模式；
3. **agent 选择**：chat 会话不显示 agent 选择器（固定 default 轻量）；task 会话保持现有 agent 选择；
4. **附件提示**：chat 会话上传附件/图片时提示「当前为聊天模式，附件相关任务请新建任务会话」（可一键新建对应任务会话并带上附件）；
5. **定时任务**：chat 会话隐藏「创建定时任务」入口。

**不做**：会话内模式切换按钮、会话内 ws 重建逻辑（模式锁定，见 §1）。

---

## 5. 改动清单

| 文件 | 改动 |
| --- | --- |
| `acp_agent.py` | `build_agent` 按 context.mode 裁剪 tools/skills/subagents/system_prompt；新增 `CHAT_SYSTEM_PROMPT` 常量；缺省 task |
| `api/history.py` | 无需改动（dict 透传），仅文档说明 `mode` 字段 |
| `api/timers*.py` | 创建定时任务时校验会话为 task 模式（后端兜底） |
| `web/src/pages/ChatPage.vue` | 新建双入口、模式徽标、chat 隐藏 agent/定时任务入口、附件提示 |
| `web/src/locales/{zh,ja,en}.ts` | 新增聊天/任务相关文案（三语） |

---

## 6. 验证方案（实施时执行）

1. **后端**：`build_agent(context(mode="chat"))` → 断言 tools=None、skills=None、subagents=None、system_prompt 含聊天提示且不含"工作目录/内置文件工具"；`mode="task"`/缺省 → 与现状一致（对比改动前输出）；
2. **定时任务拦截**：chat 会话创建定时任务 → 后端拒绝；
3. **端到端**：新建聊天会话 → 发消息正常回复（无工具调用）；新建任务会话 → 现有任务能力不受影响；两会话各自模式锁定（列表 mode 字段正确）；
4. **回归**：已有会话（无 mode 字段）打开 → 行为与改动前完全一致。

---

## 7. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| chat 会话误以为能操作文件 | 聊天系统提示明确禁止 + 引导新建任务会话；无任何工具可调用 |
| 已有会话行为变化 | mode 缺省 task，已有会话零改动 |
| 定时任务与 chat 冲突 | 前端隐藏入口 + 后端兜底校验 |
| 模式锁定过死（用户想升级） | 按用户确认执行；后期如需可在会话列表加「基于此会话新建任务会话」入口（复制上下文） |

回滚：`acp_agent.py` 单文件 revert 即恢复；前端改动独立提交可独立 revert。
