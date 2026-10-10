# 手动上下文控制（Context Control）设计方案

> 版本：v1 draft
> 日期：2026-10-10
> 状态：待确认后实施
> 关联：`docs/DESIGN_ZH.md`、`docs/DESIGN_2.0_MANAGER.md`、`api/history.py`、`acp_agent.py`、`utils/moofile_util.py`

---

## 1. 背景与目标

当前历史记录已从 localStorage 切换为 moofile（展示态，见 `api/history.py`），而 Agent 对话上下文的权威来源是 langgraph SQLite checkpoint（`db/agent_state.sqlite`）。

本方案要解决的核心诉求：**「推送什么给模型」由用户手动控制**，而不是完全交给自动保存机制。典型场景：

- 只让模型看到最近 N 轮对话（裁剪）
- 过滤工具调用噪音（只保留 human/ai 文本）
- 注入额外的 system 指令
- 编辑 / 删除某条历史消息后，后续对话使用修改后的上下文
- 加载历史会话时选择「完全重放」或「仅恢复存档」

**设计原则**

1. SQLite checkpoint 是**存档层**（自动保存，唯一真相），但不是唯一恢复路径。
2. 「自动保存」与「手动推送」不冲突：自动保存的是手动控制**之后**的结果。
3. 推送层与存档层通过「运行前快照注入」衔接，互不污染。
4. 全体功能不变，改动以增量方式落地。

---

## 2. 核心矛盾与澄清

### 2.1 矛盾点

- 想「手动控制推送历史」；
- 但上下文由「自动保存的 SQLite checkpoint」维护，似乎没有手动插手的空间。

### 2.2 澄清

**checkpoint 不是"只增不改的日志"，而是"可写状态快照"。**

- 「自动保存」= 每次运行结束把**当前状态**落盘（断电/崩溃不丢进度）。
- `update_state` 编辑 / 删除消息后，**后续 prompt 用的就是修改后的状态**；已删除的消息不会因自动保存而复活。
- 因此「手动删改」与「自动保存」**不冲突**——自动保存的正是手动控制之后的结果。

**真正需要额外能力的只有一种需求**：*推送裁剪，但存档保留全部*（例如只推最近 5 轮、存档仍存全部历史）。此时需要「以指定消息快照运行」的入口（见第 6 节）。

---

## 3. 目标架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        前端（web/src）                          │
│  moofile 展示态（渲染/列表/编辑）         手动推送入口（裁剪/注入）│
└───────────────┬──────────────────────────────────┬──────────────┘
                │ PUT/DELETE 消息（编辑/删除）       │ session/prompt（带快照）
                ▼                                  ▼
┌──────────────────────────────┐   ┌──────────────────────────────────┐
│   FastAPI 桥（app.py）        │   │   ACP agent 子进程（acp_agent.py）│
│  ┌────────────────────────┐  │   │  ┌────────────────────────────┐ │
│  │ api/history.py         │  │   │  │ deepagents_acp server.py   │ │
│  │  · GET/PUT/DELETE 会话 │  │   │  │  · prompt()                │ │
│  │  · 消息级 CRUD（新增）  │◄─┼───┼──│  · 「快照运行」入口（新增）   │ │
│  └──────────┬─────────────┘  │   │  └─────────────┬──────────────┘ │
│             │                │   │                │                │
│  utils/context_util.py       │   │  create_deep_agent(...)         │
│  · get_context              │   │  checkpointer=AsyncSqliteSaver   │
│  · update_message           │   │                │                │
│  · delete_message           │   │                ▼                │
└─────────────┬────────────────┘   │  ┌────────────────────────────┐ │
              │                     │  │  db/agent_state.sqlite     │ │
              │                     │  │  （langgraph checkpoint）   │ │
              │                     │  └────────────────────────────┘ │
              ▼                     └──────────────────────────────────┘
  db/history/conversations.bson
  （moofile 展示态，仅 UI）
```

### 3.1 职责边界

| 层 | 载体 | 职责 | 可写性 |
|---|---|---|---|
| 存档层（权威上下文） | `db/agent_state.sqlite`（langgraph checkpoint） | 每次运行自动保存完整消息状态；断点恢复 | 运行自动写；`update_state` 手动改 |
| 展示态 | `db/history/conversations.bson`（moofile） | 前端渲染、会话列表/标题、便携迁移 | 前端持久化 |
| 推送层 | 内存态（运行前快照） | 决定「这次给模型看什么」 | 用户手动控制 |

---

## 4. 能力矩阵

| 诉求 | 机制 | 是否影响存档 |
|---|---|---|
| 编辑某条消息 | `update_state` 按 id 替换 | 生效（改后即用） |
| 删除某条消息 | `RemoveMessage(id=...)` | 生效（不复活） |
| 只推最近 N 轮 / 过滤工具噪音 / 注入 system | 「快照运行」入口：读全部 → 裁剪 → 注入 → 运行 | 存档完整保留 |
| 恢复历史会话 | `session/load`（checkpoint 自动）或「快照运行」手动 | — |

---

## 5. 存储层设计

### 5.1 会话标识映射

```
前端会话 id（conversation.id，形如 web-1700000000000）
        │  session/new 返回的 sessionId
        ▼
ACP session id（agentSessionId，存入 conversation.agentSessionId）
        │
        ▼
langgraph thread_id：{"configurable": {"thread_id": <session_id>}}
```

- `session/new`：`callAcp('session/new', { sessionId: conversation.id, ... })` → 返回 `sessionId` 存 `agentSessionId`。
- `session/load` / `session/prompt`：使用 `agentSessionId ?? conversation.id`。
- checkpoint 读取/写入一律使用**同一个 thread_id**（ACP session id）。

### 5.2 checkpoint 中的消息

- 位置：`aget_tuple(config).checkpoint["messages"]`，为 LangChain message 对象数组（human / ai / tool）。
- 序列化：每条 `msg.to_json()` → 与 langgraph message 格式**完全一致**（零转换）。
- 特性：
  - 追加容易（add_messages reducer）；
  - 删除必须用 `RemoveMessage`（来自 `langgraph.graph.message`，langgraph 1.2.14 已核实）；
  - 编辑：传同 id 新消息，reducer 按 id 合并替换。

### 5.3 moofile 展示态（现状，不重设计）

- 集合 `conversations`，一条会话一个对象（`id/title/updatedAt/messages[]`）。
- `messages` 内为展示态结构（`segments/process/finalText`），**不与 checkpoint 消息格式互换**。
- 消息级编辑/删除时，需**同步**更新 moofile 展示态，保证 UI 与上下文一致。

---

## 6. 后端原语：`utils/context_util.py`（新建）

核心模块，封装所有 langgraph 交互，上层（api）不直接接触 checkpoint 细节。

```python
# 伪代码签名（实现时以真实 API 为准）

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import RemoveMessage

DB_PATH = os.path.join(os.getenv("DB_ROOT", "./db"), "agent_state.sqlite")

def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}

async def open_saver() -> AsyncSqliteSaver:
    conn = await aiosqlite.connect(DB_PATH)
    return AsyncSqliteSaver(conn)

async def get_context(session_id: str) -> list[dict]:
    """读取 checkpoint 全部消息，返回 langgraph message JSON 列表（最新优先无需排序）。"""
    saver = await open_saver()
    try:
        tup = await saver.aget_tuple(_config(session_id))
        if not tup:
            return []
        return [m.to_json() for m in tup.checkpoint.get("messages", [])]
    finally:
        await saver.close()

async def update_message(session_id: str, message_json: dict) -> None:
    """按 id 替换一条消息（编辑）。"""
    saver = await open_saver()
    try:
        msg = _message_from_json(message_json)   # 还原为 LangChain message 对象
        await saver.aupdate_state(_config(session_id),
                                  {"messages": [msg]}, as_node="...")   # as_node 见风险点 8.1
    finally:
        await saver.close()

async def delete_message(session_id: str, message_id: str) -> None:
    """删除一条消息（RemoveMessage，reducer 真正移除）。"""
    saver = await open_saver()
    try:
        await saver.aupdate_state(_config(session_id),
                                  {"messages": [RemoveMessage(id=message_id)]},
                                  as_node="...")
    finally:
        await saver.close()

async def run_with_snapshot(session_id: str, messages: list[dict], /, *,
                            system: str | None = None) -> None:
    """「快照运行」：以指定消息列表作为本次运行输入（见第 7 节），
    运行结果仍写回原 checkpoint（存档保留全部）。"""
    # 实现依赖 deepagents_acp「快照运行」入口（第 7 节）
    ...
```

**关键点**

- `as_node`：`aupdate_state` 需要指定写入节点名；deepagents 内部 graph 节点名需实测，不匹配时改用 saver 底层 `checkpoint.put` 或 deepagents_acp 包内部入口。
- 并发：agent 子进程写（WAL），FastAPI 只读/低频写——SQLite WAL 支持并发读；写侧需处理 `busy_timeout`。
- 幂等：消息 id 不变，重放/重复调用安全。

---

## 7. deepagents_acp「快照运行」入口

### 7.1 现状

`deepagents_acp/server.py` 的 `prompt()` 内部为：

```python
async for stream_chunk in agent.astream(
    {"messages": [{"role": "user", "content": content_blocks}]},
    config=config,
    stream_mode=["messages", "updates"],
    subgraphs=True,
):
```

即 `astream` **本身已支持传完整 messages 数组**，只是 ACP 协议层只暴露「单条用户消息」。

### 7.2 改造（两种路径，二选一）

**路径 A（推荐）：不改第三方包，在 app.py 桥接层封装**

- `app.py` 的 `/acp-ws` 转发层维护每个会话的「运行前消息列表」注入点。
- 前端 `session/prompt` 时可选携带 `context_override`（消息 JSON 数组 + system），桥接层将其与 ACP 请求一并转发，agent 侧按 override 运行。
- 优点：不 fork 包，升级无痛；缺点：桥接层逻辑稍复杂。

**路径 B：fork / patch deepagents_acp**

- 在 `prompt()` 增加可选参数（如 `prefill_messages`），内部直接用给定数组替换默认单条 user 消息后 `astream`。
- 优点：语义最清晰；缺点：绑定第三方包内部结构，升级需同步。

### 7.3 快照运行语义

```
输入：messages（用户裁剪/注入后的列表）+ system（可选）
执行：agent.astream({"messages": messages, ...}, config, ...)
输出：ACP session/update 事件照常推送（前端渲染不变）
存档：运行结束后 langgraph 自动落盘「运行后状态」（含本次新消息）
      —— 注意：若裁剪了历史，checkpoint 中旧消息仍在（add_messages 追加语义）
```

> 注：如果「裁剪推送」的同时希望**存档也按裁剪后为准**，则先 `update_state` 清空/重建 messages 再运行——两个语义分开暴露，由上层决定。

---

## 8. API 契约

在 `api/history.py` 基础上扩展（前缀 `/api/history`，沿用现有 `router`）。

### 8.1 读取上下文

```
GET /api/history/{session_id}/context
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `limit` | int, 可选 | 只返回最近 N 条消息 |
| `drop_tools` | bool, 可选 | 过滤 tool 消息（仅 human/ai） |
| `system` | str, 可选 | 附加 system 指令（放在列表头部） |

响应：

```json
{
  "sessionId": "web-1700000000000",
  "context": [
    { "type": "system", "content": "…" },          // 仅当传了 system
    { "type": "human", "content": "…" },
    { "type": "ai", "content": "…", "tool_calls": [] },
    { "type": "tool", "tool_call_id": "t-1", "content": "…" }
  ]
}
```

### 8.2 编辑消息

```
PUT /api/history/{session_id}/messages/{message_id}
```

请求体：新的 langgraph message JSON（与 checkpoint 格式一致）。

行为：

1. `context_util.update_message(...)` 更新 checkpoint；
2. 同步更新 moofile 展示态中对应消息（若存在）；
3. 返回 `{ok: true}`。

### 8.3 删除消息

```
DELETE /api/history/{session_id}/messages/{message_id}
```

行为：

1. `context_util.delete_message(...)`（RemoveMessage）；
2. 同步删除 moofile 展示态对应消息；
3. 返回 `{ok: true}`。

### 8.4 快照运行（二期）

```
POST /api/history/{session_id}/replay
```

请求体：

```json
{
  "messages": [ … ],        // 手动构造/裁剪后的消息列表
  "system": "…",            // 可选
  "prompt": "继续"           // 可选：运行时的用户输入
}
```

行为：走第 7 节「快照运行」入口，返回流式事件（或桥接至现有 WS 通道）。

---

## 9. 前端对接

### 9.1 编辑消息（已有功能改造）

- 现有「编辑消息」入口（编辑不重跑）保存时：
  1. `PUT /api/history/{session_id}/messages/{mid}` 更新 checkpoint + 展示态；
  2. 保留本地 UI 立即更新；
  3. 提示用户：修改将影响后续对话上下文。

### 9.2 删除消息（新增）

- 消息 `...` 菜单新增「删除消息」：
  1. `DELETE /api/history/{session_id}/messages/{mid}`；
  2. 本地移除该消息并同步展示态；
  3. 二次确认提示。

### 9.3 手动上下文控制（二期）

- 加载历史会话时，默认 `session/load`（checkpoint 自动恢复）；
- 高级选项「以指定上下文继续」：`GET /context` → 用户裁剪（轮数/过滤工具/注入 system）→ `POST /replay`。

---

## 10. 实施阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| 0 | 新建 `utils/context_util.py`：get_context / update_message / delete_message | 单测：读 → 编辑 → 重读验证 → 删除 → 重读验证 |
| 1 | `api/history.py` 扩展：context 读、消息 PUT/DELETE | API 冒烟测试（GET/PUT/DELETE） |
| 2 | 前端编辑保存走 PUT；新增删除消息入口 | 前端 build + 手动流程：编辑/删除后重开会话上下文一致 |
| 3 | 「快照运行」入口（app.py 桥接或包内） | 裁剪推送 + 存档保留完整；再 `session/prompt` 用新上下文 |
| 4 | 同步文档（README/DESIGN） | 文档与实现一致 |

---

## 11. 风险点与注意事项

1. **`as_node` 适配**：`aupdate_state` 需指定节点名，deepagents 内部节点（model/tools 等）需实测；不匹配时改 saver 底层写入。**风险：中，耗时点。**
2. **编辑语义**：改/删消息后，checkpoint 中后续消息仍基于旧上下文生成；手动推送时以修改后 messages 为准（重放机制用途），前端需提示影响。
3. **并发**：FastAPI 与 agent 子进程同写一个 SQLite——WAL 并发读 OK；写侧建议 `busy_timeout` 重试，避免 `database is locked`。
4. **大对象**：checkpoint 消息可能含 tool artifact / base64 图片，序列化注意体积；`GET /context` 可加 `limit` 兜底。
5. **快照运行与自动存档**：默认「裁剪推送 → 存档完整」；如需「存档同步裁剪」需先 `update_state` 重建 messages——两个语义分开暴露，勿混用。
6. **langgraph 版本**：当前 1.2.14；`RemoveMessage` 位于 `langgraph.graph.message`（不在顶层 `langgraph.graph`）。升级 langgraph 时需回归验证。

---

## 12. 后续升级预留

- **多 Agent 上下文隔离**：thread_id 已是会话级；若按 Agent 分桶，可在 config metadata 中扩展 `agent` 键。
- **上下文模板**：`system` 注入模板化（按会话/按 Agent 配置），与 `agents.json` 联动。
- **审计**：checkpoint 版本化（checkpoint_id）天然可回溯；如需「编辑历史」审计日志，可增量记录 update/delete 操作到 moofile 独立集合。
- **跨会话上下文复用**：`GET /context` 结果可直接作为新会话「快照运行」输入，实现「把 A 会话的上下文搬进 B 会话」。
