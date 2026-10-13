# 会话上下文独立存储（Session Context）设计方案

> 版本：v1
> 日期：2026-10-13
> 状态：✅ 已实施（2026-10-13，一期后端闭环）
> 演进：`docs/control_update.md`（v1 draft，被本方案替代——原「运行前快照注入」思路演进为「上下文表权威」）
> 关联：`utils/moofile_util.py`、`utils/session_context.py`（新建）、`api/history.py`、`api/context.py`、`utils/context_compactor.py`、`acp_agent.py`、`deepagents_acp/server.py`、`.env`

---

## 0. 实施记录（2026-10-13）

### 0.1 已落地内容

| 项 | 说明 |
|---|---|
| `utils/session_context.py`（新建） | moofile 表 `db/session_content.bson` 的读写封装：`get_context` / `save_context`（upsert）/ `init_from_checkpoint` / `update_message` / `delete_message` / `rollback_last_turn`；进程内 `threading.Lock` + 跨进程 Windows 文件锁（`msvcrt.locking`）双保险 |
| `utils/context_compactor.py` | 拆出纯函数 `compact_messages_list(messages) -> (compacted_list, result_dict)`；`compact_messages`（checkpoint 版）保留为 legacy 路径 |
| `acp_agent.py` `RollbackAgentServer.prompt` | 重写为「上下文表权威」流程：读表（无记录从 checkpoint 初始化）→ 表级压缩 → 同步 checkpoint（`RemoveMessage(ALL)` + 表内容，`as_node="__start__"`）→ 调 `AgentServerACP.prompt`（直连基类，绕开 CompactingAgentServer 的 checkpoint 压缩）→ 写回表；取消/重试全部落在表上 |
| `api/context.py` | `usage` / `compact` 改以上下文表为口径（表无记录时从 checkpoint 一次性初始化回退） |

### 0.2 与草案的关键差异（实现修正）

1. **废弃 4.2 的「改 `deepagents_acp/server.py` + prefill_messages 透传」**：
   - `deepagents_acp` 是 site-packages 第三方包（v0.0.12），直接改会被升级覆盖；
   - 更根本的原因：`DeepAgentState.messages` 用 `DeltaChannel(_messages_delta_reducer)` 合并，**未提及的历史消息不会被替换只会追加**——prefill 传压缩列表时 checkpoint 保留旧全量，运行后写回会把表冲回「未压缩全量 + 摘要」，压缩失效且消息膨胀。
2. **改为「同步 checkpoint」路径**：prompt 前 `aupdate_state(config, {messages: [RemoveMessage(REMOVE_ALL_MESSAGES), *表内容]}, as_node="__start__")`，然后走原逻辑（原 astream 只追加新 user 消息，不重复）。该模式与既有 `_rollback_checkpoint` / `compact_messages` 同构，已生产验证。压缩后**必须**同步 checkpoint（同一次写入），否则运行后写回仍会把未压缩状态冲回表。
3. **取消语义**：表回滚到 prompt 前快照；checkpoint 复用既有 `_rollback_checkpoint`（`REMOVE_ALL_MESSAGES` + replay）。
4. **retry 语义**：`_meta.retry` 时 `rollback_last_turn` 直接删表最后 user turn，再走常规流程。
5. **消息存储格式**：采用 `BaseMessage.to_json()` 输出（lc 序列化，`kwargs.id` 承载消息 id），用 `convert_to_messages` 无损还原；草案 3.2 的平铺示例格式与 `to_json()` 实际输出不符，以本实施为准。
6. **`_root_dir` 解析**：`DB_ROOT`（绝对或相对项目根）→ 项目根 `/db`，不依赖进程 cwd（与 `api/history.py` 一致）。

### 0.3 测试结果（2026-10-13，`test_session_context_e2e.py`，30 项全过）

- 单测：get/save/upsert/init/rollback/update/delete（13 项）
- 两轮真实对话：表=checkpoint、历史累积（4 项）
- retry：表回滚最后一轮（2 项）
- 压缩：60 条 → 11 条（摘要 1 + 最近 8 条 + 本轮 2 条），压缩后表=checkpoint（4 项）
- 取消：stop_reason=cancelled、表保持快照、checkpoint 回滚（3 项）

### 0.4 会话删除 / 消息编辑与 sqlite 同步（2026-10-13 追加）

**背景**：langgraph 强制 checkpoint 落 sqlite（`agent_state.sqlite`），`messages` 只是表的镜像。此前删除/编辑只动上下文表，sqlite 里残留旧 thread，违背"可控性"目标。本次补齐三类操作的 sqlite 同步：

| 操作 | 表动作 | sqlite 动作 | 入口 |
|---|---|---|---|
| 删会话 | 删记录（`delete_session`，新增） | `DELETE checkpoints/writes WHERE thread_id=?`（幂等，`_delete_checkpoint_thread`） | `DELETE /api/context/{session_id}`（新增）；`DELETE /api/history/{cid}` 连带调用 |
| 编辑消息 | `update_message` 替换 | **作废镜像**：删该会话 thread，下次 prompt 由表重建 | `PUT /api/context/{session_id}/messages/{message_id}`（新增） |
| 删除消息 | `delete_message` 过滤 | **作废镜像**：删该会话 thread，下次 prompt 由表重建 | `DELETE /api/context/{session_id}/messages/{message_id}`（新增） |

关键设计：
- **表是权威，sqlite 是运行镜像**。消息级变更后删 thread（而非改写 blob），语义是"镜像作废、下次 prompt 自动重建"——避免直接解析 langgraph checkpoint 序列化 blob 的脆弱方案。
- **HTTP 层无法触达子进程的 checkpointer**（app.py 网关 → WS → acp_agent 子进程 stdio），故 `_delete_checkpoint_thread` 按 langgraph sqlite schema（`checkpoints` / `writes` 表，thread_id 列）直接 SQL 删除，与 `AsyncSqliteSaver.adelete_thread` 等价。
- **删除顺序**：先删 sqlite thread 再删表（防并发 prompt 从 checkpoint 把会话"复活"）。
- **id 口径**：`new_session()` 服务端生成 `uuid4().hex`（ACP 会话 id）；前端 history 的 `web-<ts>` 是展示态 id。两套 id 均被删除接口兜底（幂等，未命中则只删对应层）。

验证（`test_session_delete.py`，12 项全过 + HTTP 实测）：
- 删会话：表记录 + sqlite thread 双清；重复删除幂等返回 False
- 编辑消息：表内容已改 + sqlite thread 已作废
- 删除消息：表消息减少 + sqlite thread 已作废
- HTTP：`DELETE /api/context/{sid}` 返回 `{"ok":true,"removed":true}` 且两处均清；`DELETE /api/history/{cid}` 不误伤真实会话（上下文表记录数不变）
- e2e 回归：`test_session_context_e2e.py` 30 项仍全过

### 0.5 待办（二期）

- [ ] 前端手动上下文控制 UI（编辑/删除按钮接上述 PUT/DELETE 接口）
- [ ] 跨进程并发写压测（多 WebSocket 子进程同时写表）；实测覆盖严重时备选：上下文表改用 sqlite 或按 session 分文件

---

## 1. 背景与目标

### 1.1 现状

| 层 | 载体 | 作用 | 可写性 |
|---|---|---|---|
| 展示态 | `db/db_history_*/db.bson`（moofile） | 前端渲染、会话列表 | 前端持久化 |
| 上下文（真实喂给 AI 的历史） | `db/agent_state.sqlite`（langgraph checkpoint） | 每次 prompt 自动恢复的全部消息 | 运行自动写，手动介入困难 |

问题：
1. **上下文无法手动控制**——编辑/删除/裁剪都要绕 checkpoint（RemoveMessage、as_node 等底层机制）；
2. **压缩作用在 checkpoint**——压缩后前端展示态与真实上下文割裂，usage 显示口径混乱；
3. **取消/重试的残留**——checkpoint 自动写入半截状态，需要额外回滚逻辑（已用 RollbackAgentServer 兜底，但仍属补丁式修复）。

### 1.2 目标

> **「真实给 AI 的历史 JSON 数据」独立存储为一张 moofile 表（`db/session_content.bson`），手动控制（编辑 / 删除 / 裁剪 / 压缩）全部作用在这张表上。**

- 前端展示态与 UI **完全不动**（继续走 `db_history_*` 展示表渲染）；
- langgraph checkpoint **继续自动存档**（agent 内部状态 / 断点 / 崩溃恢复），但**不再作为下一次 prompt 的上下文来源**；
- 每次 prompt：从 `session_content` 表读历史 → 追加新用户消息 → 跑 agent → **运行后写回该表**；
- 压缩：只改写 `session_content` 表中的消息列表。

### 1.3 设计原则

1. **上下文表是唯一权威**——「下次给模型看什么」完全由表内容决定，其余存储不参与决策；
2. **展示与上下文分离**——前端怎么显示与 AI 吃什么互不污染，彻底消除「前端少显示一条」「拼接」「编辑不同步」等历史问题的根源；
3. **checkpoint 降级为运行态**——只存 agent 执行所需状态与断点，不承载「对话上下文」语义；
4. **增量落地**——一期后端闭环 + 前端零改动；二期再开放手动编辑 API 与 UI。

---

## 2. 目标架构

```
┌────────────────────────────── 前端（web/src）─────────────────────────────┐
│  展示态读取/渲染（现状，不动）         手动上下文控制（二期：编辑/删除/裁剪） │
└──────────────┬───────────────────────────────────────┬───────────────────┘
               │ GET/PUT /api/history（现状）            │ GET/PUT /api/context（二期）
               ▼                                        ▼
┌─────────────────────────────┐      ┌──────────────────────────────────────┐
│  api/history.py             │      │  api/context.py（扩展）                │
│  db_history_*/db.bson       │      │  utils/session_context.py（新建）     │
│  （展示态，现状不动）        │      │    · 读/写/初始化/编辑/删除            │
└─────────────────────────────┘      └──────────────┬───────────────────────┘
                                                    │ 读历史 + 追加 user
                                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  acp_agent.py  RollbackAgentServer.prompt                                  │
│  1. 读 session_content 表（无记录 → 从 checkpoint 一次性拷贝初始化）        │
│  2. 压缩检查（作用于表内容，压缩结果写回表）                               │
│  3. 快照（取消回滚基准）                                                   │
│  4. 调 deepagents_acp server.prompt(prefill_messages=历史+新user)          │
│  5. 运行结束 → 将结果消息写回 session_content 表                           │
│  6. 取消 → 不写回（表保持 prompt 前状态，天然防拼接）                      │
└──────────────┬──────────────────────────────────────────────┬─────────────┘
               │ prefill_messages（完整消息列表）               │ 自动存档（现状）
               ▼                                              ▼
┌────────────────────────────────────┐      ┌──────────────────────────────────┐
│ deepagents_acp/server.py prompt    │      │ db/agent_state.sqlite             │
│  · 支持 prefill_messages 分支      │      │ （langgraph checkpoint）           │
│  · astream({"messages": <完整列表>})│      │  内部状态/断点/崩溃恢复，不再作为   │
└────────────────────────────────────┘      │  下次 prompt 的上下文来源          │
                                           └──────────────────────────────────┘
```

### 2.1 职责边界（最终形态）

| 层 | 载体 | 职责 | 是否作为 prompt 上下文来源 |
|---|---|---|---|
| 展示态 | `db/db_history_*/db.bson` | 前端渲染、会话列表/标题 | 否 |
| **上下文态（权威）** | **`db/session_content.bson`** | **真实喂给 AI 的消息列表；手动控制落点** | **是（唯一）** |
| 运行态 | `db/agent_state.sqlite`（checkpoint） | agent 内部状态、断点、崩溃恢复 | 否（仅首次初始化借用一次） |

---

## 3. 存储设计

### 3.1 表位置与命名

- **路径**：`db/session_content.bson`（用户指定；`MooFileUtil(root_dir=<db目录>).create_database("session_content")`）
- 说明：用户明确平铺在 `db/` 根目录。若后续要与 moofile_base 规范完全一致，可改为 `db/session_content/db.bson + meta.json`（见 9.5 决策点，默认按用户指定执行）。
- 普通表（非向量），`is_vector=False`。

### 3.2 记录格式（每会话一条记录）

```json
{
  "session_id": "web-1700000000000",
  "messages": [
    { "id": "msg-xxx", "type": "human", "content": "你好", "additional_kwargs": {}, "response_metadata": {} },
    { "id": "msg-yyy", "type": "ai", "content": "你好！", "tool_calls": [], "additional_kwargs": {}, "response_metadata": {} }
  ],
  "message_count": 2,
  "updated_at": "2026-10-13T12:00:00+08:00"
}
```

- `messages` 为 **langgraph message 的 `to_json()` 输出**（与 checkpoint 中完全一致，零转换、可无损还原为 `BaseMessage`）；
- 一条会话一个文档：**整体读一次、整体写一次**，语义简单、天然原子（moofile 单文件 BSON）；
- 会话规模下（单机、数百会话）性能无压力；若未来单会话消息量巨大，可拆「每消息一条记录」（见 9.1）。

### 3.3 生命周期

```
会话新建（首次 prompt） → 表无记录 → 从 checkpoint 拷贝当前消息 → 写表
每次 prompt            → 读表 → 追加新 user → 跑 agent → 写回（新增 assistant/tool 消息）
手动编辑/删除/裁剪      → 直接改表
压缩                   → 读表 → 压缩算法 → 压缩结果写回表
取消                   → 不写回（表保持 prompt 前状态）
```

---

## 4. 后端改造

### 4.1 新建 `utils/session_context.py`

封装对 `db/session_content.bson` 的全部操作，上层（acp_agent / api）不接触 moofile 细节。

```python
# 签名草案（实现时以真实 API 为准）
from typing import Any

DB_NAME = "session_content"          # -> db/session_content.bson
_ROOT_DIR: Path | None = None        # 首次使用时解析为项目 db/ 目录
_LOCK = threading.Lock()             # 进程内读写串行化

def _root_dir() -> Path: ...         # 解析 db/ 目录（DB_ROOT 优先，其次项目根/db）
def _util() -> MooFileUtil: ...
def _ensure() -> None: ...           # 表不存在则 create_database

def get_context(session_id: str) -> list[dict] | None:
    """读取该会话的上下文消息列表；无记录返回 None（调用方决定是否从 checkpoint 初始化）。"""

def save_context(session_id: str, messages: list[dict]) -> None:
    """整体覆盖写回（按 session_id upsert）。"""

def init_from_checkpoint(session_id: str, checkpoint_messages: list[Any]) -> list[dict]:
    """首次初始化：将 checkpoint 的 BaseMessage 列表 to_json 后写表，返回 JSON 列表。"""

def update_message(session_id: str, message_json: dict) -> None:
    """按 id 替换一条消息（编辑）。"""

def delete_message(session_id: str, message_id: str) -> None:
    """按 id 移除一条消息（数组内删除）。"""

def rollback_last_turn(session_id: str) -> bool:
    """删除「最后一条 user 消息」及之后的所有内容（重新生成用）。"""
```

### 4.2 `deepagents_acp/server.py`：prompt 支持完整消息列表（prefill）

> ⚠️ **已废弃（2026-10-13 实施修正）**：改为「同步 checkpoint」路径，见 0.2 节。本小节保留供追溯。

`AgentServerACP.prompt` 增加可选参数 `prefill_messages`（langchain message 对象列表）：

```python
# 现状
async for stream_chunk in agent.astream(
    {"messages": [{"role": "user", "content": content_blocks}]},
    ...
):
# 改造：有 prefill_messages 时用它，否则走原逻辑
prompt_input = {"messages": prefill_messages} if prefill_messages else {"messages": [{"role": "user", "content": content_blocks}]}
async for stream_chunk in agent.astream(prompt_input, ...):
```

- 改动约 10 行，不改变现有调用行为（无 prefill 时与现状完全一致）；
- `prefill_messages` 通过 ACP 的 `_meta` 透传（`params.update(meta)` 机制已核实可用，与上轮 retry 标记同路径）。

### 4.3 `acp_agent.py`：`RollbackAgentServer.prompt` 接线（核心）

```
prompt(session_id, prompt, _meta…):
  if _meta.prefill_context:          # 前端/调用方显式提供（二期手动控制入口）
      context_messages = _meta.prefill_context
  else:
      context_messages = get_context(session_id)
      if context_messages is None:   # 首次：从 checkpoint 拷贝初始化
          state = await agent.aget_state(config)
          context_messages = init_from_checkpoint(session_id, state.values["messages"])
      # 压缩检查（作用于 context_messages，压缩结果写回表）
      if should_compact(context_messages):
          compacted = await compact_messages_only(context_messages)   # 4.4 改造
          save_context(session_id, compacted)
          context_messages = compacted
      # 追加新 user 消息
      context_messages = context_messages + [{"role": "user", "content": content_blocks}]
  # 快照（取消回滚基准 = 追加前的表内容）
  snapshot = context_messages
  result = await super().prompt(..., _meta={**meta, "prefill_messages": context_messages})
  if result.stop_reason == "cancelled":
      save_context(session_id, snapshot)        # 取消：不保留本次追加，表回到 prompt 前
      return result
  # 运行后：把本次新增消息写回表
  final_messages = await agent.aget_state(config)  # checkpoint 里最新完整消息
  save_context(session_id, to_json_list(final_messages))
  return result
```

- **取消语义**：不写回 → 表保持 prompt 前状态 → 下一次 prompt 不会拼接半截输出（**替代现有 checkpoint 回滚补丁**，更彻底）；
- **retry 语义**：`_meta.retry` 时先 `rollback_last_turn(session_id)`（直接删表内容），再走常规流程；
- **写回源**：运行后从 checkpoint 读最新消息写表——checkpoint 仍自动保存完整状态，作为「运行结果」的搬运通道，**不需要额外维护双份增量**。

### 4.4 压缩语义切换（`utils/context_compactor.py`）

- 现状：`compact_messages(agent, config, messages)` 用 `aupdate_state(as_node="__start__")` 改 checkpoint；
- 改造：拆出**纯函数** `compact_messages_list(messages) -> (compacted_list, result_dict)`——只对**消息列表**做「摘要 + 移除」，返回压缩后的列表；写回动作由调用方决定（新方案写回 session_content 表）；
- `should_compact` / `estimate_tokens` 保持；
- `api/context.py` 手动压缩接口：改为「读表 → `compact_messages_list` → 写表」，并回读表内容计算 usage（口径与前端一致）；
- 前端 context usage 显示：从「读 checkpoint」改为「读 session_content 表」（口径统一，见 5.3）。

### 4.5 `session/load`（历史会话加载）

- **一期不动**：前端加载历史仍走 `session/load`（重放 checkpoint 事件）与展示态表渲染；
- **注意**：此时展示态与上下文表可能不一致（如手动裁剪过）——一期仅后端闭环，前端看到的是展示态；二期的「加载后以上下文表为准」见 9.4。

---

## 5. 数据一致性与并发

### 5.1 写入时机

| 事件 | 动作 |
|---|---|
| 首次 prompt | 从 checkpoint 初始化写表 |
| 每次 prompt 结束 | checkpoint 最新消息 → 写表（整体覆盖） |
| 取消 | 不写（保持 prompt 前表内容） |
| 手动编辑/删除/压缩（二期） | 直接改表 |

### 5.2 并发

- **进程内**：`threading.Lock` 串行化（FastAPI 同进程内多个请求）；
- **跨进程**：FastAPI（写 API/压缩）与 acp_agent 子进程（prompt 写回）可能同时写 `session_content.bson`；
  - moofile 单文件 BSON，跨进程并发写有覆盖风险；
  - 缓解：写前读后写、短临界区 + 进程级文件锁（`fcntl`/Windows 等价物），或在 `.env` 约定「手动编辑时不运行 prompt」；
  - **默认策略（一期）**：prompt 写回与手动 API 都走同一工具函数，内部加锁；实测碰撞概率低，先实现后压测。

### 5.3 usage / 压缩阈值口径统一

- 前端 `refreshContextUsage` 显示的 token 与压缩阈值**都基于 session_content 表内容**计算（`estimate_tokens`），不再与 checkpoint 口径漂移。

---

## 6. 前端改造

**一期：完全不动。**

- 展示态仍读 `db_history_*`（现状）；
- 发送 / 取消 / 重试 / 滚动等交互逻辑不变；
- 上下文表对前端透明（`_meta` 透传是后端内部协议，前端无需感知）。

---

## 7. API 契约（二期，后端可先就绪）

前缀 `/api/context`（与现有 `api/context.py` 合并扩展）：

```text
GET    /api/context/{session_id}                     # 读取上下文表（消息 JSON 列表）
PUT    /api/context/{session_id}/messages/{mid}      # 编辑一条消息
DELETE /api/context/{session_id}/messages/{mid}      # 删除一条消息
POST   /api/context/{session_id}/rollback            # 回滚最后一条 user turn（重试）
POST   /api/context/{session_id}/compact             # 手动压缩（改造现有接口）
```

请求/响应体均为 langgraph message JSON（与表格式一致），前端二期据此做「编辑/删除/裁剪」UI。

---

## 8. 实施阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| 0 | 新建 `utils/session_context.py`（get/save/init/update/delete/rollback）+ 单测 | 增删改读回验证；无记录返回 None |
| 1 | `deepagents_acp/server.py` prefill 分支 + `acp_agent.py` 接线（读表→初始化→跑→写回→取消不写） | 连续多轮对话：表内容与 checkpoint 一致；取消后重发不拼接；重试回滚生效 |
| 2 | 压缩迁移（`compact_messages_list` 纯函数 + acp_agent 调用 + `api/context.py` 改造） | 85% 阈值触发自动压缩；压缩后表内容为压缩结果；usage 显示与表一致 |
| 3 | 二期 API（读/编辑/删除/回滚/压缩接口） | API 冒烟测试 |
| 4 | 文档同步（README / DESIGN / 本文件更新）+ 全链路回归 | 功能与文档一致 |

---

## 9. 风险与决策点

### 9.1 每会话一条记录 vs 每消息一条记录（**需拍板**，默认：每会话一条）

- 每会话一条：整体读写简单、原子性好，但单次写放大（会话大时）；
- 每消息一条：增量写，但读取需按会话聚合、删改需维护顺序，复杂度上升；
- 默认选前者，未来数据量不达标再拆分。

### 9.2 取消时 user 消息是否保留在上下文表（**需拍板**，默认：不保留）

- 不保留：表严格等于「prompt 前状态」，取消即放弃该轮，语义最干净；
- 保留：取消后 AI 知道用户曾问过（但未回答），可能引发「重复追问」；展示态始终显示该条 user，不影响视觉。

### 9.3 一期是否开放手动 API（**需拍板**，默认：一期只做后端闭环，API 随二期）

- 后端闭环（prompt 读表/写回/压缩）优先，验证稳定后再暴露编辑 API。

### 9.4 `session/load` 重放一致性（二期）

- 一期 session/load 照旧；二期需决策「加载历史时展示/上下文是否以 context 表为准」；
- 若要求严格一致，`session/load` 应改为从 context 表重建 agent 输入（prefill 语义），改动可控（复用 4.2 分支）。

### 9.5 表位置规范

- 用户指定 `db/session_content.bson`（平铺）；
- moofile_base 目录规范为 `db/<db_name>/db.bson + meta.json`——如后续统一，仅需调整 `_root_dir/_ensure` 一处，记录格式不变。

### 9.6 其他风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 跨进程并发写表 | 覆盖/丢更新 | 锁 + 短临界区；一期低频写，实测 |
| 首次初始化与 checkpoint 竞争 | 初始化拿到半截状态 | 初始化只在「表无记录」时发生，且发生在 prompt 前快照点 |
| 大对象（tool artifact/图片 base64） | 表体积膨胀 | 与 checkpoint 同源，不额外放大；压缩时过滤 tool 细节 |
| 压缩与 checkpoint 不同步 | 崩溃后从 checkpoint 恢复的是未压缩全量 | checkpoint 仅为断点；恢复后由「首次初始化」逻辑重新对齐（表优先，缺失才从 checkpoint 补） |
| langgraph 升级 | `to_json` 格式变化 | 用 `BaseMessage.from_json` 双向转换，跟随库版本回归 |

---

## 10. 与 `docs/control_update.md` 的关系

- control_update.md 的「checkpoint 权威 + 运行前快照注入」在本方案中**被替换为「上下文表权威」**：
  - 原方案需要同时维护「存档层」与「推送层」两种语义（裁剪推送/存档完整），复杂度高；
  - 新方案把「真实给 AI 的历史」实体化为一张表，手动控制与压缩天然落点，checkpoint 只管运行态；
  - 原方案第 7 节「快照运行」入口与本方案 4.2 prefill 分支等价，实现路径被保留。
- 本文档生效后，control_update.md 标记为「已由 session_context.md 替代」。

---

## 11. 待确认清单（实施前）

- [ ] 表记录粒度：每会话一条（默认）还是每消息一条
- [ ] 取消时 user 消息：不保留（默认）还是保留
- [ ] 手动 API：一期就绪还是二期随 UI
- [ ] 表位置：`db/session_content.bson`（用户指定，确认执行）
- [ ] 压缩阈值/usage 口径统一从表读（默认执行）
