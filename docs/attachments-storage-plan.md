# 附件（图片/文件）统一存储方案（MooFile 附件库）

> 版本：v2（v1 → v2：方案落地为实际实现，新增前端统一上传、文件占位、base64 迁移、报错堆栈）
> 日期：2026-10-14
> 状态：✅ 已实施（提交：`ed03e8f`、`e3e02cf`、`56f7ac5`、`5be39d6`）
> 关联：`utils/attachment_store.py`、`utils/session_context.py`、`api/attachments.py`、`api/context.py`、`api/history.py`、`.deepagents/tools/mcp_servers/browser_server/browser_mcp_server.py`、`web/src/pages/ChatPage.vue`、`web/src/components/MarkdownMessage.vue`、moofile_base 存储规范

---

## 0. 目标与决策

**目标**：AI 产出的图片（浏览器截图、生成图、上传图）在会话履历中可显示、可追溯；文件移动/整体迁移不失效；会话 token 不被 base64 撑爆。

**核心决策**（承接上一轮讨论）：

1. **元数据进 MooFile，blob 进附件目录**——不把图片二进制塞进 BSON 库，与对象存储"索引 + blob"同构。
2. **引用存 `file_id`（逻辑 ID），不存绝对路径**——物理位置由附件库唯一管理，迁移 = 搬目录，引用永不失效。
3. **LLM 上下文只放占位符**（`[图片: {file_id}]`），不读像素。
4. **存储规范与 moofile_base 完全对齐**——新增附件库目录 `db/db_attachments_<hash8>/`，结构与 `db_history_*` 相同，且复用 moofile_base 的 `_upload/` 附件约定，moofile_base UI 可直接看到。

---

## 1. 存储规范（新增附件库）

```
db/
├── db_history_<hash8>/          # 现有：会话列表/消息（不动）
├── db_session_content_<hash8>/  # 现有：上下文表（不动）
├── db_attachments_<hash8>/      # 新增：附件库
│   ├── db.bson                  # 附件元数据记录（每附件一条）
│   ├── db.bson.meta             # MooFile 内部元数据（自动生成）
│   ├── meta.json                # {"name": "attachments", "description": "会话附件（图片/文件）"}
│   └── _upload/                 # blob 物理文件，文件名 = {file_id}.{ext}
└── agent_state.sqlite           # 现有：langgraph checkpoint（不动）
```

- 目录前缀 `db_attachments_` 满足 moofile_base `list_db_dirs()` 的 `db_*` 发现规则 → **moofile_base UI 统一可见**。
- `_upload/` 与 moofile_base documents 功能的附件目录同构，命名规则一致。
- **物理文件命名 = file_id**：`_upload/{file_id}.png`。记录与文件一一对应，无重名、无绝对路径依赖。
- 迁移/备份 = 整体搬 `db_attachments_*` 目录，随 `db/` 一起走。
- 真实实例：`db/db_attachments_74760fdf/`（首次上传自动创建，含 `.attachments.lock` 跨进程锁）。

> ⚠️ **上线提醒**：`api/attachments.py` 是新增路由，**旧后端进程不会加载**——改完附件代码必须重启服务（`python app.py`），否则前端上传会得到 404/405 并报"文件上传失败"。

---

## 2. 附件记录结构（db.bson 每条记录）

```json
{
  "file_id": "3f9a1b2c...",              // uuid4().hex，唯一
  "session_id": "web-1728...",           // 归属会话（级联删除 + 访问鉴权）
  "message_id": "b1c2d3...",             // 归属消息（可空 = 会话级附件）
  "kind": "image",                       // image / audio / file
  "filename": "shot_20261014_0825.png",  // 展示名（下载/预览用）
  "storage_path": "_upload/3f9a....png", // 相对附件库目录
  "mime": "image/png",
  "size": 123456,
  "width": 1920, "height": 1080,         // 可选，前端布局用
  "alt_text": "浏览器截图：DeepAgents 工作台", // LLM 上下文占位描述
  "created_at": "2026-10-14T08:25:00+08:00"
}
```

---

## 3. 消息中的引用（token 控制的关键）

- 消息 `content` 中放置轻量占位：**`[图片: {file_id}]`** / **`[文件: {file_id}]`**（模型易生成、markdown 兼容、前端易替换）。
- **LLM 上下文**：占位原样保留（≈10 token/张），不读像素；用户消息在占位后附带 `附件路径：<绝对路径>`，模型需要读图/读文件时走现有本地文件工具，无需吃像素。
- **前端渲染**：渲染时把占位替换为 `<img src="/api/attachments/{file_id}">` 或附件下载链接（见 §5）。
- **双保险**：附件表以 `message_id` 关联，前端也可按消息批量拉取附件清单，不依赖文本解析。

---

## 4. 接口层（新建 `api/attachments.py`，挂 `/api/attachments`）

| 接口 | 方法 | 说明 |
|---|---|---|
| `POST /api/attachments` | 注册 | 表单上传（file + session_id + message_id? + kind? + alt_text?）→ 存 `_upload/` + 写记录 → 返回 `{file_id, url: "/api/attachments/{file_id}", attachment:{..., path:<绝对路径>}}`（`path` 供模型本地文件工具读取） |
| `GET /api/attachments/{file_id}` | 读取 | `FileResponse`，Content-Type 按 mime；校验 session 归属；**前端 `<img>` 与未来 CDN 切换的唯一入口** |
| `GET /api/attachments?session_id=...` | 列表 | 返回该会话全部附件（按 message_id 分组），会话详情加载时用 |
| `DELETE /api/attachments/{file_id}` | 删除单条 | 删记录 + 物理文件（幂等） |
| `DELETE /api/attachments?session_id=...` | 级联删除 | 会话删除时调用：删全部记录 + 物理文件 |

**鉴权**：`GET /{file_id}` 校验 `session_id` 归属（防止跨会话枚举）；本地应用可放宽为仅校验会话存在。

**报错契约**：存储异常返回 `500 {detail: "store failed: <err>\n<traceback>"}`——**完整堆栈随响应返回**，前端会把 detail 原样带进错误信息（AI 消息 / 错误条 / console.error），不再出现"文件上传失败。"这种无信息报错。

---

## 5. 代码改动点

| 文件 | 改动 |
|---|---|
| **`utils/attachment_store.py`（新建）** | 附件库封装，对齐 `session_context.py` 风格：`_root_dir()`（`ATTACHMENT_ROOT` env → `DB_ROOT` → 项目根/db，自动发现/创建 `db_attachments_<hash8>`）、进程锁 + 文件锁、`register(file, session_id, message_id, kind, alt_text)` / `get(file_id)` / `list_by_session(session_id)` / `delete(file_id)` / `delete_by_session(session_id)` / `resolve_abs(file_id)` |
| **`api/attachments.py`（新建）** | §4 五个接口；`app.py` 挂载 router |
| **`browser_server/browser_mcp_server.py`** | `browser_screenshot` 加 `session_id` 参数（可选）：截图落盘改到附件库 `_upload/`（通过 `attachment_store.register`），返回 `[图片: {file_id}]` 占位 + 绝对路径——模型可凭 abs 路径用文件工具读图 |
| **`api/context.py`** | `DELETE /api/context/{session_id}` 中追加：`attachment_store.delete_by_session(session_id)`（先删 sqlite thread → 再删上下文表 → 最后删附件，保持既有删除顺序语义） |
| **`utils/session_context.py`** | `delete_message` / `update_message` 中追加：级联删该 `message_id` 的附件（`attachment_store.delete_by_message(message_id)`）；编辑消息时若 `[图片: id]` / `[文件: id]` 占位被移除，级联删对应附件（`_cascade_removed_images`） |
| **`web/src/components/MarkdownMessage.vue`** | 渲染前正则预处理：`[图片: {id}]` → `<img src="/api/attachments/{id}">`；`[文件: {id}]` → `[📎 附件](/api/attachments/{id})` 下载链接 |
| **`web/src/pages/ChatPage.vue`** | **前端统一上传**：粘贴/拖拽/屏幕截屏的图片与文件在发送前一律 `uploadResource` 上传附件库，消息与持久化只保留轻量 ref（`file_id/url/path/name/mimeType/kind`）；prompt 组装 `[图片: id]`（附 `附件路径：...`）/ `[文件: id]` 占位；**旧 base64 图片在消息回放/重试时自动转回 File 迁移入库**；已有 `file_id` 直接复用不重传；上传失败时把后端 detail（含堆栈）透传给 AI 消息与错误条 |

**不做**（控制范围）：
- 不迁移 `browser_server/screenshots/` 历史截图（旧消息中 "Screenshot saved: ..." 纯文字保持原样）。
- 不新增"读图给模型"的专用工具——模型需要分析图片时，凭返回的绝对路径走现有文件读取工具即可。

---

## 6. 级联删除链路（与既有联动一致）

```
删除会话  DELETE /api/context/{sid}
   ├─ 1. sqlite thread 删除（既有 _delete_checkpoint_thread）
   ├─ 2. 上下文表记录删除（既有 delete_session）
   └─ 3. 【新增】附件库：删 session_id 全部记录 + _upload 物理文件

删除消息  DELETE /api/context/{sid}/messages/{mid}
   ├─ 1. 上下文表过滤消息（既有 delete_message）
   ├─ 2. sqlite thread 作废（既有）
   └─ 3. 【新增】附件库：删 message_id 关联记录 + 物理文件

编辑消息  PUT .../messages/{mid}
   ├─ 内容替换（既有 update_message）
   └─ 【新增】附件归属不变；若消息里的 `[图片: id]` / `[文件: id]` 占位被移除，级联删对应附件（_cascade_removed_images）
```

---

## 7. 验证方式（已执行 ✅）

| 层 | 验证 | 结果 |
|---|---|---|
| 单测 | `test_attachment_store.py`（23 项） | ✅ 全过 |
| 接口 | `test_attachment_api.py`（19 项，临时 DB_ROOT 隔离） | ✅ 全过 |
| 真实实例 | 运行中的 8000 服务 `POST /api/attachments`（真实 `db_attachments_74760fdf`）→ 200 + file_id；`GET /{file_id}?session_id=` → 200 返回 PNG 字节一致；测试附件已清理 | ✅ |
| 前端 | `vite build` 通过；截图/粘贴/文件上传链路在运行实例上验证 | ✅ |
| token | 上下文 JSON 中图片仅以 `[图片: id]` 存在，无 base64（用户消息另附绝对路径提示） | ✅ |

---

## 8. 实施顺序（实际执行轨迹）

1. ✅ `utils/attachment_store.py` + 单测（`ed03e8f`）
2. ✅ `api/attachments.py` + `app.py` 挂载 + 接口测试（`ed03e8f` / `e3e02cf`）
3. ✅ `browser_screenshot` 接入（改落盘 + 注册 + 返回 file_id + 占位）
4. ✅ `api/context.py` / `session_context.py` 级联删除（`ed03e8f`）
5. ✅ 前端统一上传：`MarkdownMessage.vue`（[图片]/[文件] 渲染）+ `ChatPage.vue`（uploadResource + 占位 + base64 迁移 + 轻量持久化）（`e3e02cf`）
6. ✅ 删除会话弹窗改为居中 NModal（`56f7ac5`）
7. ✅ 报错保留堆栈（后端 detail + traceback；前端透传）（`5be39d6`）
8. ✅ 全链路 e2e + 重启服务加载新路由 + 提交推送
