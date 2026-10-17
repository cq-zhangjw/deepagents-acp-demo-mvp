# Todo 工具方案（任务分步状态管理）

> 状态：已确认方向，待实施（用户已批准：任务模式强制 + 状态持久化）
> 关联：docs/chat-task-mode-plan.md（chat/task 模式分离，未实施；本方案默认先全局生效，模式分离落地后收敛为 task 模式强制）

## 1. 背景与目标

Agent 执行任务时，当前只有 `process.plan`（计划展示）与 `toolCalls` 记录，**没有可交互、可更新的分步状态**。目标：

1. 任何**任务型请求**执行前，Agent 先调用 `create_todo` 建立分步清单；
2. 每完成一步，Agent 调用 `update_todo` 更新该步状态；
3. 全部步骤完成后，Agent 在最终回复中汇总；
4. 前端把 todo 渲染为可勾选的进度清单，会话刷新/续跑后状态不丢。

**已确认的边界**：
- 仅任务型（task 模式）强制；chat 模式不强制（当前模式分离未实施，先全局生效，落地 chat/task 分离后自动收敛）；
- 状态持久化（挂会话级存储，刷新/压缩/续跑不丢）。

## 2. 工具接口设计

实现方式：**内置 langchain StructuredTool**（追加到 `build_agent` 的 tools 列表，与 MCP 工具同构），不新增 MCP server——纯状态操作，无需子进程。

| 工具 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `create_todo` | `items: [{title: str, description?: str}]` | `{todo_id, items[]}` | 创建分步清单；todo_id 由工具生成（`t_<hex8>`） |
| `update_todo` | `todo_id, step_index: int, status: 'pending'\|'in_progress'\|'completed'\|'blocked', note?: str` | 更新后全量清单 | 推进某一步；支持回退（completed→in_progress） |
| `get_todo` | `todo_id` | 当前清单 | 恢复/查询用 |

状态机：`pending → in_progress → completed`（允许 `blocked` 暂停并说明原因）。

## 3. 强制流程（系统提示词）

`acp_agent.py get_system_prompt()` 基础提示词第 1 条改写为：

```
1. For task requests: before acting, call create_todo to break the task into
   steps. Then execute step by step; after each step, call update_todo to mark
   it completed (or blocked with a reason). When all steps are done, summarize
   the results in your final reply.
```

- 全局生效（chat/task 未分离时的过渡策略）；
- chat 模式分离落地后，chat 用轻量提示词（无此规则）自然豁免。

## 4. 状态存储

- 存储：`db/todos/<todo_id>.json`（每个 todo 一个文件，独立于会话历史）；
- 文件结构：`{todo_id, session_id, title?, items: [{index, title, description?, status, note?, updatedAt}], createdAt, updatedAt, completedAt?}`；
- 后端只读 API：`GET /api/todos/{todo_id}`（跨会话按 id 读文件，不鉴权——todo_id 即访问凭证）；
- **会话关联**：前端在 ACP 事件流里捕获 `create_todo` 的返回 `todo_id`，写入 `conversation.todoId`（走既有 `saveConversation` 持久化）；
- 压缩兼容：todo 是独立文件，上下文压缩不触碰；压缩摘要可引用"当前进行到第 N 步"（后续增强）。

## 5. 前端渲染考虑

### 5.1 数据流（增量，不整段重绘）

ACP 事件流已推送 `process.toolCalls`（含工具名/参数/返回）。前端复用该流：

1. **create_todo 事件** → 解析 `items` + 返回 `todo_id` → 生成本地 `todoState`（Map<todoId, items>）→ 写入 `conversation.todoId` 并 `saveConversation`；
2. **update_todo 事件** → 按 `todo_id + step_index` 更新本地状态（含 note/blocked）；
3. 消息渲染时：todoId 存在 → 在消息的 **执行过程面板（ProcessCollapse）内、plan 区上方**渲染 todo 勾选清单。

### 5.2 渲染形态

- 位置：`ProcessCollapse` 内新增 `todo` 区块（在 plan 列表之上，二者不重复——todo 有则展示勾选清单，plan 保留原展示）；
- 样式：步骤行 = 状态图标 + 标题 + （note） + 时间：
  - `completed` → ✓ 绿色勾、标题划线淡化
  - `in_progress` → ● 蓝色点、加粗
  - `pending` → ○ 空心圆
  - `blocked` → ⚠ 橙黄警示 + note
- 折叠：复用 ProcessCollapse 折叠机制；会话级 todo 面板独立可折叠；
- 移动端：≤720px 与现有执行面板同策略（图标保留、紧凑行高）。

### 5.3 会话恢复

- 打开历史会话时：`conversation.todoId` 存在 → `GET /api/todos/{todo_id}` → 成功则渲染持久化清单（并更新本地状态）；404（todo 已被清理）→ 静默忽略；
- 进行中会话刷新：同上恢复路径，进度不丢。

### 5.4 与现有元素的关系

| 元素 | 关系 |
| --- | --- |
| `process.plan` | 保留原样（模型仍可输出 plan）；todo 面板在上方优先展示分步状态 |
| `process.toolCalls` | todo 状态完全从该事件流派生，无需后端推送新事件类型 |
| 上下文压缩 | todo 独立文件不受影响；摘要可含"进度 N/M"（本期不做，二期增强） |

## 6. 实施清单

1. `api/todos.py`：TodoStore（create/update/get/read，写 `db/todos/`）+ `GET /api/todos/{todo_id}` 只读接口 + `app.py` 挂载；
2. `acp_agent.py`：
   - 追加 3 个 langchain StructuredTool（create_todo/update_todo/get_todo，操作 TodoStore，异常返回 Error 字符串）；
   - `build_agent` tools 列表 `+ todo 工具`；
   - `get_system_prompt` 第 1 条改写（强制流程）；
3. `api/manage/manager.py`：`BUILTIN_TOOLS` 增补 `todo` 展示项（description "Manage task step checklist"）——默认 agent inner_tools 全量自动含 todo，UI 可开关；
4. `web/src/pages/ChatPage.vue`：
   - `TodoItem` 类型 + `todoStates` ref；
   - 解析 toolCalls 中 create_todo/update_todo；
   - 消息渲染 ProcessCollapse 内 todo 面板（勾选清单）；
   - `conversation.todoId` 持久化 + 恢复拉取；
   - i18n 三语文案（todo 面板标题/状态标签/工具名展示）；
5. 验证：后端单测（TodoStore CRUD + 状态机 + 文件持久化）；构建；Playwright（任务执行展示 todo 面板、步骤更新、刷新恢复）；回归分组/导出功能。

## 7. 风险与兼容

| 风险 | 应对 |
| --- | --- |
| 简单任务也被强制建清单（变重） | 提示词措辞限定 "task requests"；chat/task 分离后收敛到 task 模式 |
| 旧会话无 todoId | 渲染逻辑 todoId 缺失即不显示面板，零影响 |
| todo 文件累积 | 会话删除时顺带清理 todo 文件（delete_history 钩子，二期）；一期仅随文件自然存在 |
| MCP/内置工具开关 | todo 进 BUILTIN_TOOLS，UI 可关；关闭后提示词规则仍要求——模型调用失败会走普通 plan，可接受（规则措辞为"when the tool is available"） |
| 与压缩冲突 | 独立文件，无冲突 |

## 8. 验证与回滚

- 验证：见第 6 节；Playwright 覆盖：task 会话内发起任务 → 出现 todo 面板 → 步骤状态随 toolCalls 更新 → 刷新后恢复；
- 回滚：git revert 单 commit（后端/前端各一）；todo 文件无副作用（删文件即清）。
