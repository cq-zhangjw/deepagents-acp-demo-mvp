# 同一窗口多会话并行运行 —— 实现方案（规划稿）

> 状态：**规划中**（本文件仅记录方案与改动点，尚未实施）
> 适用版本：当前 main（含绝对路径链接、定时任务面板、管理弹窗体系等近期改动）
> 目标读者：后续执行本方案的开发者 / Agent

---

## 1. 背景与目标

### 1.1 现状

- 当前**每个打开的浏览器标签页/窗口 = 一条 WebSocket（`/acp-ws`）= 后端一个独立的 ACP agent 子进程**，天然支持"多窗口并行"。
- 但**同一窗口内**：前端维护**单条** `ws` 连接（单例），同一时刻只有一个"活跃会话"在跑；切换会话只切换 UI 与历史，agent 侧通过 `session/load` / `session/new` 按 `sessionId` 切换上下文。

### 1.2 目标

在**同一个窗口内**让多个会话**同时运行**（如豆包 / Claude 的多标签并行）：

- 会话 A 正在执行任务（流式输出中），切到会话 B 发消息，两者互不干扰；
- 各会话保持自己独立的 agent 子进程、上下文（checkpointer）、工作目录、权限请求；
- 消息、流式事件、权限请求都正确路由到对应会话的 UI。

### 1.3 已确认的事实结论（供决策）

- **"注释掉 ws.close" 不可行**：切换会话（`selectConversation`）本来就不关闭 ws；`ws.close()` 只发生在**切换 agent**（`selectAgent` / `resetAgentToDefault`），因为 `/acp-ws?agent=xxx` 参数不同、必须重建 agent 子进程。注释会导致切 agent 不生效。
- 后端**无需改动**：`/acp-ws` 每条连接天然独立子进程（见 2.1）。
- 数据库**无需改动**：会话历史按 `conversation.id` 独立 upsert，不同会话并行无冲突（见 6）。

---

## 2. 现状架构（关键事实与代码位置）

### 2.1 后端 —— 每条 WS 连接一个独立 agent 子进程（已支持）

`app.py`

| 位置 | 内容 |
| --- | --- |
| 160-162 | `@app.websocket("/acp-ws")`，docstring：*"Each WebSocket connection corresponds to an independent ACP agent subprocess, forwarding messages bidirectionally."* |
| 166 | `agent_name = websocket.query_params.get("agent", "default")` —— WS URL 参数决定 agent |
| 169-173 | `asyncio.create_subprocess_exec(AGENT_CMD, ..., stdin/stdout/stderr=PIPE)` —— 每次连接新建子进程 |
| 180-208 | `forward_stdout()`（子进程 stdout → WS）、`forward_stderr()`、`forward_ws()`（WS → 子进程 stdin）双向转发 |
| 214, 223-234 | WS 断开时 `_terminate_process_tree(proc)` 杀整个进程树（防 agent 泄漏） |

结论：**前端每建一条 WS，后端就多一个独立 agent 进程**。多会话并行只需前端多建 WS。

### 2.2 后端 agent 子进程 —— 消息级 sessionId 路由（已支持）

`acp_agent.py`

- 导入 `deepagents_acp.server.AgentServerACP, AgentSessionContext`；
- `AgentSessionContext` 构建 DeepAgent，**每个 session 独立状态与工作目录**（217 行注释）；
- `AsyncSqliteSaver` checkpointer：session 状态持久化到 `agent_state.sqlite`；
- `AgentServerACP(load_sessions=True)`：支持 `session/load` 续接历史会话。

结论：**同一个 agent 子进程可服务多个 sessionId**（`session/new` / `session/load` / `session/prompt` 消息级路由）。这是"前端切换会话不重连"能工作的基础，也是"每会话一条 WS"各自独立的基础。

### 2.3 前端 —— 单条 ws 单例（需改造）

`web/src/pages/ChatPage.vue`

| 位置 | 内容 | 与并行改造的关系 |
| --- | --- | --- |
| 162 | `const ws = ref<WebSocket | null>(null)` | 单例 ws，需改为按会话维护 |
| 1735 | `websocketUrl()` 返回 `/acp-ws?agent=${agent}` | URL 由当前 agent 决定 |
| 1739-1744 | `connectAgent()`：若 `ws` 已 OPEN 则复用，否则 `new WebSocket(websocketUrl())` | 复用逻辑需按会话拆分 |
| 1773 | `socket.onmessage = (event) => handleAcpMessage(event.data)` | 事件处理需带会话上下文 |
| 1778-1787 | `callAcp(method, params)`：`socket.send({jsonrpc, id, method, params})` | 需绑定到某会话的连接 |
| 1792 | `ws.value?.send({jsonrpc, id, result})`（权限/工具确认响应） | 同上 |
| 1826 | `payload.method === 'session/request_permission'` | 权限请求需路由到对应会话 UI |
| 1845 | `payload.method === 'session/update'` → `handleSessionUpdate(...)` | 流式事件需路由到对应会话 UI |
| 1945 | `switch (update.sessionUpdate)` | 会话更新处理（无会话区分） |
| 1257-1266 | `resetAgentToDefault()`：close ws + `ws.value = null` | **保留**：切 agent 必须重连 |
| 1336-1348 | `selectAgent(key)`：close ws + `ws.value = null` | **保留**：切 agent 必须重连 |
| 2000-2024 | `ensureAgentSession(conversation, isNew)`：`session/new` / `session/load`，`initializedSessions` Set | 需按会话维护各自的状态 |
| 2414-2422 | 取消（session/cancel）后 close ws 杀进程 | 需改为只影响当前会话的连接 |

前端状态（与会话绑定、需改造的单例）：

- `ws`、`socketPromise`、`connected`、`initializedSessions`、`permissionRequest`、流式缓冲等均按当前单一活跃会话假设实现。

---

## 3. 为什么"注释 ws.close"不是答案

- 切换会话（`selectConversation`，510 行起）**从不关闭 ws**：它只切换 `currentConversation`、拉历史、重置窗口缓冲；真正发消息时由 `ensureAgentSession` 走 `session/load`（旧会话恢复）或 `session/new`（新会话），**同一条 ws、同一个 agent 子进程按 sessionId 换上下文**。
- `ws.close()` 只出现在**切换 agent**（`selectAgent` 1336 / `resetAgentToDefault` 1257）：agent 不同 → WS URL 的 `agent` 参数不同 → 必须重建子进程。注释后切换 agent 仍连旧进程，agent 不生效。
- 因此"注释 close"既不是并行所需的改动，还会破坏切换 agent。

---

## 4. 目标设计

### 4.1 总体思路

前端把"单条 ws 单例"改为**按会话维护独立连接**：

```
Map<conversationId, SessionConnection>
SessionConnection = {
  ws: WebSocket,
  socketPromise: Promise<WebSocket> | null,
  connected: boolean,
  initialized: boolean,
  initializedSessions: Set<string>,   // agentSessionId 初始化标记（原 ensureAgentSession 用）
  pendingRequests: Map<requestId, { resolve, reject }>,  // 若需要按连接隔离请求表
}
```

- 每个会话首次发消息时建立自己的 `ws`（`/acp-ws?agent=<该会话上次使用的 agent>`），后端即启动独立 agent 子进程；
- 会话切换只改"当前显示"（`currentConversation`），**不关闭任何连接**；
- 切换 agent：**只重建当前会话的连接**（新 agent 参数），其他会话连接不受影响；
- 收到 WS 事件：按"该连接对应的 conversationId"路由到对应会话的 UI 状态（消息追加、assistant 状态、权限弹窗、tool calls）。

### 4.2 会话 → agent 的绑定

- 每个会话需要记住自己使用的 agent（建议存会话对象字段 `agent?: string`，默认 `default`；新建会话固定 `default`，切换 agent 后更新当前会话的 agent 字段并持久化）；
- 连接 URL：`/acp-ws?agent=${conversation.agent}`；
- 切换 agent 时：更新当前会话 agent 字段 → 关闭该会话连接 → 下次发消息按新 agent 重建。

### 4.3 事件路由

ACP 事件（`session/update`、`session/request_permission`、`session/request_input`）由各连接自己的 `onmessage` 回调直接绑定会话 id，在回调内部把事件应用到对应会话的 UI：

- `handleAcpMessage(data)` 改为 `handleAcpMessageFor(conversationId, data)`；
- `handleSessionUpdate(update)` 改为接收 `(conversationId, update)`，内部所有对"当前会话"的引用改为按 id 取目标会话对象；
- 权限请求 `permissionRequest` 需改为 `Map<conversationId, permission>`，弹窗只展示当前会话的权限请求，拒绝/允许响应发往对应连接；
- 流式缓冲（assistant 消息分段累积）按会话 id 定位。

---

## 5. 前端改造清单（执行时按此推进）

1. **建立连接管理模块**
   - 抽出 `SessionManager`（或组件内一个 `Map` + 若干 helper）：
     - `getConnection(conversationId): SessionConnection`（不存在则创建）
     - `closeConnection(conversationId)`（供切 agent / 删除会话 / 取消时用）
     - `closeAll()`（组件卸载 / 页面关闭时）
   - 原 `connectAgent` / `socketPromise` / `connected` 逻辑迁入，按会话隔离。

2. **发消息链路**
   - `sendMessage`：目标会话 `ensureAgentSession` 后，通过该会话连接 `callAcp('session/prompt', ...)`；
   - `callAcp` 增加会话参数：`callAcpFor(conversationId, method, params)`；
   - 响应（`result` / `error`）按 `id → 连接` 反查，勿全局共享请求表。

3. **事件链路**
   - `onmessage` 在连接创建时闭包绑定 `conversationId`；
   - `handleSessionUpdate` / 权限处理按会话 id 路由；
   - `currentMessages` / 当前 assistant / tool calls 均按会话 id 读取与更新。

4. **切换 agent**
   - `selectAgent(key)`：更新**当前会话**的 `agent` 字段 → `closeConnection(currentId)` → 清当前会话 `initializedSessions` → 下次发送重建连接；
   - `resetAgentToDefault` 同理（作用于当前会话）。

5. **取消 / 删除会话**
   - 取消：`session/cancel` 发给对应会话连接后 `closeConnection(conversationId)`（保持后端杀进程语义：`app.py` 在 WS 关闭时 `_terminate_process_tree`）；
   - 删除会话：先 `closeConnection(id)`，再删历史（现有 `/api/history/{id}` DELETE 不变）。

6. **UI 状态**
   - 右侧工具栏、顶部"已连接"指示建议改为**当前会话的连接状态**（或保持全局 + 当前会话两个维度）；
   - 会话列表可在运行的会话旁显示轻量状态点（可选增强，非必须）。

7. **清理**
   - 组件卸载（路由离开 / `onUnmounted`）时 `closeAll()`，避免 agent 子进程泄漏。

---

## 6. 数据库并发说明（无需改动）

- 会话历史按 `conversation.id` 独立 upsert（`PUT /api/history/{id}`），不同会话并行各写各的记录，**无冲突**；
- 定时任务每次执行独立 `agentSessionId`、独立写记录，与会话并行无冲突；
- **注意**：同一会话在多个标签页同时操作会互相覆盖（后写覆盖先写）——并行改造只解决"同窗口多会话"，不解决"同会话多端并发编辑"（不建议支持）。

---

## 7. 验证方案

1. **同一窗口两会话并行**
   - 会话 A 发一个耗时任务（如"读取整个项目并总结"）；
   - A 运行中切到会话 B 发另一个任务；
   - 期望：A、B 各自流式输出，互不串扰；切回 A 能看到 A 继续/完成的结果；权限请求只弹当前会话的。

2. **切换 agent 仍生效**
   - 当前会话 A 从 default 切到子 agent，发消息；
   - 期望：agent 确实切换（工具面板、行为变化），且其他会话的连接不受影响。

3. **取消**
   - 会话 A 运行中取消 → 只关 A 的连接（后端杀 A 的进程），B 不受影响。

4. **进程数与泄漏**
   - 开 N 个会话并各发消息 → 后端应有 N 个 `acp_agent.py` 子进程；
   - 删除会话 / 切 agent / 关页面 → 对应子进程退出（任务管理器核对）。

5. **历史恢复**
   - 重启前端后进入某会话 → `session/load` 恢复上下文，消息历史与 agent 上下文一致。

---

## 8. 风险与回滚

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 事件串台 | 事件路由遗漏某条路径时，输出会串到其他会话 | 事件回调一律带 conversationId 闭包；验证方案 1 重点覆盖 |
| 连接/进程泄漏 | 关闭页面或删除会话不关 WS，后端 agent 子进程残留 | `onUnmounted` / 删除 / 取消时统一 `closeAll/closeConnection`；验证方案 4 |
| 请求表冲突 | 响应按全局 id 反查时跨会话串扰 | 请求表按连接隔离 |
| 内存上升 | 会话越多子进程越多（每会话一个 Python 进程） | 属于预期成本；本地模型为吞吐瓶颈（见 9） |
| 切 agent 破坏 | 改造后若沿用旧的全局 `initializedSessions`，切 agent 上下文错乱 | 初始化标记按会话隔离，切 agent 时清当前会话 |

回滚：改动集中在前端连接管理与事件路由两层，保持原 `ws` 单例代码路径作为 fallback（或在 git 上单提交，便于 revert）。

---

## 9. 资源与吞吐说明（背景事实）

- 每个并行会话 = 一个 Python agent 子进程 + 一份模型上下文窗口；
- 若模型走本地推理服务（当前 `.env`: `ENDPOINT=http://192.168.3.28:8088`, `MODEL_NAME=qwen3.8-9b`），**真正瓶颈是推理服务**：多会话并发请求时本地单实例会排队，表现为整体变慢而非报错；
- 若模型 API 吞吐足够（云端/多实例），并行体验不受影响。

---

## 10. 实施顺序建议

1. 前端抽出 `SessionManager`（Map 化连接），先跑通"每会话一条 WS、事件按会话路由"的最小闭环（发消息 → 流式 → 切会话 → 回切）；
2. 补切 agent / 取消 / 删除 / 卸载清理四条路径；
3. 按第 7 节验证清单逐项验证；
4. 全部通过后再考虑会话列表"运行中"状态点等增强。
