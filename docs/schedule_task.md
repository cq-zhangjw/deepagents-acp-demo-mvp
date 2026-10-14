# 会话持久化改造 & 定时任务方案（Schedule & Task）

> 状态：方案已拍板，进入实施。本文档为设计与决策记录，代码实现以本文件为准。
> **实施进度（2026-10-14 核对）**：
> - 阶段一「会话持久化改造（后端权威存储）」**已实装**（`/api/history` GET/PUT/DELETE + MooFile `db/history`，前端进会话强制 GET、不推送历史、不再依赖 localStorage，`utils/moofile_util.py` 封装）。
> - 阶段二「定时任务」**已实装**：后端 `api/timers_engine.py`（MooFile `db/db_timer_tasks_*` 数据层 + 无人值守 ACP 执行引擎 + 调度器）+ `api/timers.py`（CRUD/手动执行/履历 API）+ `app.py` 挂载与 lifespan 启动调度器；前端右侧面板 `timers` tab（时钟入口：任务列表/搜索/新建/编辑/启停/立即执行/删除/执行记录）；`croniter` 依赖已入 venv；`.env` / `.env.bak` 增加 `TIMER_TASKS_ROOT` / `TIMER_ENABLED` / `TIMER_TICK_SECONDS`。测试与验证清单见文末「实施验证记录」。

## 1. 背景与目标

当前会话历史由**前端全量 PUT 同步**到 moofile（`db/history/conversations.bson`），存在三个问题：

1. **全量 PUT 语义"以我为准、列表外删除"**——后台（定时任务、未来任何服务端写入）往会话追加消息后，会被前端下一次 PUT 覆盖，无法支持服务端写会话履历。
2. **网络请求量大**——流式期间每 300ms 一个 `PUT /api/history`，网络面板刷屏。
3. **定时任务落地受阻**——没有后台写会话的安全通道。

**目标**：把会话持久化改为**后端权威存储**——前端只读 + 关键节点提交；为定时任务提供"执行结果写回会话履历"的能力。

## 2. 已拍板决策（Decision Log）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 会话持久化模型 | 后端为唯一权威副本；前端只在关键节点按会话粒度提交，不再全量同步 |
| D2 | 前端提交 API | 会话级 upsert：`PUT /api/history/{cid}`（整会话覆盖式合并），非消息级 append |
| D3 | 删除语义 | 显式 `DELETE /api/history/{cid}`，废除全量 PUT 的隐含删除 |
| D4 | 会话加载 | 每次进入会话**强制 GET** `/api/history/{cid}`，不做缓存 |
| D5 | 实时可见性 | v1 不做"正在查看的会话实时刷新"；后台写入后用户下次进会话（或刷新）自然可见 |
| D6 | 定时任务呈现 | 每个任务**绑定一个会话**（默认自动创建专用会话，可选绑定现有会话），执行履历写入该会话；任务自身定义与执行记录存 `db/timer_tasks` |
| D7 | 定时任务 user 消息 | 纯标记：`【定时任务】{任务名} · {触发时间}`，不带 prompt 原文（prompt 在任务面板可见，避免上下文重复） |
| D8 | 无人值守策略 | 定时执行遇 `request_permission` / `request_input` 自动拒绝并记 `failed`，不等待人工 |
| D9 | 任务重叠 | 上一轮执行未结束，下一轮到点跳过（记 `skipped`） |
| D10 | 服务重启 | 重启时把 `running` 记录补 `cancelled`；调度器启动后按 cron 重新计算下次触发 |
| D11 | 并发写 | 单进程 `threading.Lock` 包住 moofile 写入；定时任务与手动会话写冲突按 last-write-wins，建议任务使用专用会话从源头避免 |
| D12 | 调度方式 | cron 表达式（`croniter`，本地时区）与间隔分钟（`intervalMinutes`）二选一 |
| D13 | 环境变量 | `TIMER_TASKS_ROOT` / `TIMER_ENABLED` / `TIMER_TICK_SECONDS`，同步写入 `.env` 与 `.env.bak` |

## 3. 阶段一：会话持久化改造（后端权威存储）

### 3.1 后端 API 变更（`api/history.py`）

| 端点 | 现状 | 改后 |
|---|---|---|
| `GET /api/history` | 返回全部会话（倒序） | 保留，语义 = 会话列表（侧栏用） |
| `GET /api/history/{cid}` | 无 | 新增，返回单会话完整消息 |
| `PUT /api/history` | 全量 upsert + **删除列表外缺失项** | **移除**（前端不再调用） |
| `PUT /api/history/{cid}` | 无 | 新增，单会话 upsert（存在则整会话覆盖合并，不存在则创建） |
| `DELETE /api/history/{cid}` | 单会话删除 | 保留，语义不变 |

- moofile 写入统一加 `threading.Lock`（单进程足够；为阶段二定时任务写会话做准备）。
- 移除"全量删除缺失项"是**冲突根源**，必须删。

### 3.2 前端提交策略（`web/src/pages/ChatPage.vue`）

废除 `persistConversations` 的防抖全量 PUT，改为关键节点**立即提交**：

| 节点 | 动作 |
|---|---|
| 新建会话 / 重命名 / 编辑消息 / 删除消息 | 立即 `PUT /api/history/{cid}` |
| 发送消息（user + assistant 骨架已入内存） | 立即 `PUT /api/history/{cid}` |
| AI 流式结束（completed / cancelled / failed，含工具调用最终态） | 立即 `PUT /api/history/{cid}` |
| 流式中间过程（chunk、工具调用进行中） | **不提交**，仅本地内存 |
| 删除会话 / 清理空会话 | 显式 `DELETE /api/history/{cid}`（或对目标逐条 DELETE） |
| localStorage 一次性迁移 | 保留：启动 GET 列表为空且 localStorage 有旧数据时，逐会话 PUT 导入后清 key |

提交实现：`void fetch('/api/history/' + cid, { method: 'PUT', body: JSON.stringify(conversation) })`，失败静默（内存态保留，下次节点重试）。

### 3.3 加载策略

- 启动 / 刷新：`GET /api/history`（列表）。
- 进入会话：`GET /api/history/{cid}` **强制拉取**，用服务端数据替换本地该会话（**每次**，不做缓存）。
- 流式保护：**当前正在运行的会话**（存在 streaming / pending / waiting_permission 的 assistant 消息）进会话时**直接用内存态**，不 GET 覆盖——避免流式中断/回滚显示。仅非运行会话才强制 GET。
- 未加载的会话（不在本地）点击时 GET 后加入列表。

### 3.4 已知限制（阶段一）

- 流式中途刷新页面：未结束的中间文本不恢复（现状同样存在，可接受）。
- 删除会话后刷新前若发生 PUT（该会话已被剔除，前端列表不再包含）——删除后立即显式 DELETE，前端内存同步剔除，不会再有该会话的 PUT。

## 4. 阶段二：定时任务

### 4.1 数据模型（moofile，`db/timer_tasks`，库 `tasks`）

```json
{
  "id": "timer_xxx",
  "name": "每日早报",
  "prompt": "生成今日行业早报…",
  "schedule": "0 8 * * *",
  "intervalMinutes": null,
  "sessionId": "conv_xxx",
  "enabled": true,
  "lastRunAt": 1700000000000,
  "lastStatus": "success | failed | running | skipped",
  "runHistory": [
    {
      "id": "run_1",
      "startedAt": 1700000000000,
      "finishedAt": 1700000000100,
      "status": "success | failed | cancelled | skipped",
      "outputSummary": "…",
      "assistantMessageId": "msg_xxx"
    }
  ],
  "createdAt": 1700000000000,
  "updatedAt": 1700000000000
}
```

### 4.2 调度器

- FastAPI lifespan 启动 asyncio 后台任务，每 `TIMER_TICK_SECONDS`（默认 30）tick 一次。
- 扫描 `tasks` 库 `enabled=true` 的任务：
  - `schedule` 非空：`croniter` 计算 next fire，命中即触发；
  - `intervalMinutes` 非空：`now >= lastRunAt + intervalMinutes * 60000` 触发。
- 重启自动恢复（每次 tick 现算，不持久化 next_fire）。
- 执行重叠：任务级互斥——上一轮未结束则本轮跳过（记 `skipped`）。

### 4.3 执行引擎

- **进程内复用现有装配**：`from acp_agent import build_agent` + `AgentServerACP(agent=..., load_sessions=True)`，在 `asyncio.to_thread` 中调用 `await server.prompt([TextContentBlock(text=prompt)])`，与手动对话同一条 agent 链路、同一 checkpointer（sqlite）。
- 无人值守：执行期间如产生 `request_permission` / `request_input`，自动拒绝并记 `failed`（不等待人工）。
- 手动立即执行：`POST /api/timers/{id}/run` 复用同一执行路径（同步等待结果返回）。

### 4.4 会话履历写入（后端直接写）

```
tick 命中
  → ① 写开始：会话 append user 消息「【定时任务】{name} · {触发时间}」
      + assistant 占位（pending）；timer_tasks.runHistory 写 startedAt=running
  → ② 进程内执行 AgentServerACP.prompt()
  → ③ 写结束：assistant 更新为最终文本 + 状态（completed/failed）
      + runHistory 写 finishedAt / status / outputSummary
  → ④ 写入走 moofile conversations 库（query → messages push → update，加锁）
```

前端感知 = 用户下次进入该会话时强制 GET（D4），天然一致，无需通知机制（D5）。

### 4.5 前端入口与面板

- 入口：`composer-toolbar-right` 发送按钮左侧加时钟图标 → `openRightPanel('timers')`。
- 右侧面板新增 `timers` tab（沿用"从哪个入口进就显示哪部分内容"的现状，不加标签栏）：
  - 任务列表（启用在前 + 关键字过滤）
  - 新建 / 编辑：名称、prompt、调度（cron 或间隔分钟）、绑定会话（默认自动创建专用会话）
  - 启停开关、删除（解除绑定，会话保留）
  - 手动立即执行一次
  - 最近执行记录（开始 / 结束时间、状态、摘要）

### 4.6 API（`api/timers.py`，前缀 `/api/timers`）

- `GET /api/timers` —— 任务列表
- `POST /api/timers` —— 新建（自动创建专用会话时同时建会话）
- `PUT /api/timers/{id}` —— 更新（名称 / prompt / 调度 / 启停 / 绑定会话）
- `DELETE /api/timers/{id}` —— 删除（解绑会话）
- `POST /api/timers/{id}/run` —— 手动立即执行
- `GET /api/timers/{id}/history` —— 执行履历

### 4.7 环境变量

```
TIMER_TASKS_ROOT=./db/timer_tasks
TIMER_ENABLED=true
TIMER_TICK_SECONDS=30
```

## 5. 已知限制与后续优化

1. **实时刷新**（D5）：正在查看的会话不自动刷新定时结果；后续可加轻量 WS 通知。
2. **并发写**（D11）：定时任务与手动对话共用同一会话时 last-write-wins；推荐任务使用专用会话。
3. **上下文权重**：定时任务 user 消息为纯标记，agent 会话上下文不含任务 prompt 原文。
4. **失败重试**：v1 不自动重试，失败仅记录，可后续加"失败重试 N 次"。
5. **完成通知**：v1 仅写会话 + 面板状态，无声音 / 桌面通知。
6. **多 agent 关联**：任务 v1 固定 default agent，后续可关联 agents.json 中的装配名。

## 6. 测试与验证清单（阶段一）

- [ ] 后端：`GET /api/history` 列表、`GET /{cid}` 单会话、`PUT /{cid}` 新建与覆盖、`DELETE /{cid}` 删除、404 / 空会话行为
- [ ] 前端：新建会话 → 刷新恢复；发送消息 → AI 流式 → 完成后刷新恢复（含工具调用、失败、取消态）
- [ ] 前端：删除会话 → 刷新不残留；重命名 / 编辑消息 → 刷新恢复
- [ ] 前端：流式中切走再切回 → 不被历史覆盖（内存态保护）
- [ ] 网络面板：流式期间无 history PUT 请求
- [ ] 旧数据迁移：清空后端 + localStorage 有旧数据 → 首次启动导入一次

---

## 实施验证记录（2026-10-14，阶段一与阶段二）

### 阶段一（会话持久化）验证

- [x] 前端：新建会话 → 刷新恢复；发送消息 → AI 流式 → 完成后刷新恢复（含工具调用、失败、取消态）
- [x] 前端：删除会话 → 刷新不残留；重命名 / 编辑消息 → 刷新恢复
- [x] 前端：流式中切走再切回 → 不被历史覆盖（内存态保护）
- [x] 网络面板：流式期间无 history PUT 请求
- [x] 旧数据迁移：清空后端 + localStorage 有旧数据 → 首次启动导入一次

### 阶段二（定时任务）验证

后端（隔离环境单测 + 8010 真实实例 e2e）：

- [x] `py_compile` 通过：`app.py` / `api/timers.py` / `api/timers_engine.py`
- [x] 数据层 CRUD：save / get / list（enabled 过滤）/ delete；`runHistory` 追加与 50 条截断
- [x] 调度判断 `_is_due`：cron 命中（`* * * * *` 在 10:00:30 命中、`0 8 * * *` 在 10:00 不命中）；interval（首次立即、5 分钟内不触发、5 分钟后触发）
- [x] 会话履历写入：`append_timer_start`（user 标记 + assistant pending 占位）→ `append_timer_end`（覆盖为 completed + finalText），消息结构对齐前端 `Conversation` / `AssistantMessage`
- [x] 最终文本提取 `_extract_ai_text`：AIMessage 纯文本 / 多段 content 列表
- [x] API：POST（自动建专用会话并写 history，侧栏可见）/ GET 列表 / PUT 更新 / DELETE / GET history / 404；schedule 与 intervalMinutes 二选一校验
- [x] 真实调度触发：`TIMER_TICK_SECONDS=10` 起 8010 实例，intervalMinutes=1 任务与 `* * * * *` cron 任务均按周期自动触发；无人值守执行经模型端点返回 `success`，`outputSummary` / `lastStatus` / `runHistory` 正确回写
- [x] 手动执行 `POST /api/timers/{id}/run`：同步返回 `run.status=success`
- [x] 会话履历可见：`GET /api/history` 返回 `timer-*` 专用会话（标题 `【定时任务】xxx`），消息含 user 触发标记与 assistant 最终文本
- [x] 重启恢复（D10）：`recover_running` 把遗留 `running` 补 `cancelled`

前端（playwright headless + 浏览器实测）：

- [x] composer 发送按钮左侧时钟入口 → 右侧面板 `timers` tab（标题"定时任务"）
- [x] 面板：搜索框 + 新建按钮；任务卡片（名称 / prompt / 调度标签 / 状态标签 / 上次执行时间）
- [x] 新建弹窗（名称 / prompt / cron-间隔切换 / 启用开关）→ 保存 → 列表刷新出现
- [x] 删除（确认弹窗）→ 列表移除；编辑 / 立即执行 / 执行记录入口齐全
- [x] 修复项：`reactive` 未 import 导致白屏（`ReferenceError: reactive is not defined`）；新建任务默认调度方式修正为 cron（原逻辑对空任务误走 interval）

### 已知说明

- 定时任务专用会话（`timer-*`）由后端直接写入 history，前端展示为只读履历；发送端验证时该会话因无真实 ACP session 会提示 `unverifiable session kept`（非致命，lazy-recreated 仅在用户手动发送时触发）。
- 任务名/触发文本中的中文在 PowerShell 直传 JSON 时会因终端编码损坏为 `??`（服务端 UTF-8 数据正常）；通过 Python / 前端 UI 写入无此问题。
