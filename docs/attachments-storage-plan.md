# 附件（图片）统一存储方案（MooFile 附件库）

> 版本：v1
> 日期：2026-10-14
> 状态：📋 待实施
> 关联：`utils/moofile_util.py`、`utils/session_context.py`、`api/context.py`、`api/history.py`、`browser_server/browser_mcp_server.py`、`web/src/components/MarkdownMessage.vue`、moofile_base 存储规范

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

- 消息 `content` 中放置轻量占位：**`[图片: {file_id}]`**（模型易生成、markdown 兼容、前端易替换）。
- **LLM 上下文**：占位原样保留（≈10 token/张），不读像素。需要 alt_text 时格式化为 `[图片: {file_id}]（{alt_text}）`。
- **前端渲染**：渲染时把占位替换为 `<img src="/api/attachments/{file_id}">`（见 §5）。
- **双保险**：附件表以 `message_id` 关联，前端也可按消息批量拉取附件清单，不依赖文本解析。

---

## 4. 接口层（新建 `api/attachments.py`，挂 `/api/attachments`）

| 接口 | 方法 | 说明 |
|---|---|---|
| `POST /api/attachments` | 注册 | 表单上传（file + session_id + message_id? + kind?）→ 存 `_upload/` + 写记录 → 返回 `{file_id, url: "/api/attachments/{file_id}", alt_text}` |
| `GET /api/attachments/{file_id}` | 读取 | `FileResponse`，Content-Type 按 mime；校验 session 归属；**前端 `<img>` 与未来 CDN 切换的唯一入口** |
| `GET /api/attachments?session_id=...` | 列表 | 返回该会话全部附件（按 message_id 分组），会话详情加载时用 |
| `DELETE /api/attachments/{file_id}` | 删除单条 | 删记录 + 物理文件（幂等） |
| `DELETE /api/attachments?session_id=...` | 级联删除 | 会话删除时调用：删全部记录 + 物理文件 |

**鉴权**：`GET /{file_id}` 校验 `session_id` 归属（防止跨会话枚举）；本地应用可放宽为仅校验会话存在。

---

## 5. 代码改动点

| 文件 | 改动 |
|---|---|
| **`utils/attachment_store.py`（新建）** | 附件库封装，对齐 `session_context.py` 风格：`_root_dir()`（`ATTACHMENT_ROOT` env → `DB_ROOT` → 项目根/db，自动发现/创建 `db_attachments_<hash8>`）、进程锁 + 文件锁、`register(file, session_id, message_id, kind, alt_text)` / `get(file_id)` / `list_by_session(session_id)` / `delete(file_id)` / `delete_by_session(session_id)` / `resolve_abs(file_id)` |
| **`api/attachments.py`（新建）** | §4 五个接口；`app.py` 挂载 router |
| **`browser_server/browser_mcp_server.py`** | `browser_screenshot` 加 `session_id` 参数（可选）：截图落盘改到附件库 `_upload/`（通过 `attachment_store.register`），返回 `Screenshot saved: {file_id} (path: {abs})`——模型可凭 abs 路径用文件工具读图 |
| **`api/context.py`** | `DELETE /api/context/{session_id}` 中追加：`attachment_store.delete_by_session(session_id)`（先删 sqlite thread → 再删上下文表 → 最后删附件，保持既有删除顺序语义） |
| **`utils/session_context.py`** | `delete_message` / `update_message` 中追加：级联删该 `message_id` 的附件（`attachment_store.delete_by_message(message_id)`） |
| **`web/src/components/MarkdownMessage.vue`** | markdown-it 渲染管线中追加占位替换：`[图片: {id}]` → `<img src="/api/attachments/{id}">`（正则预处理 content，再走原 sanitize 管线；img 加 `loading="lazy"`、`max-width:100%`） |
| **`web/src/pages/ChatPage.vue`** | 会话详情加载时调 `GET /api/attachments?session_id=...`，缓存 `file_id → 附件元数据`，供消息组件渲染 |

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
   └─ 【新增】附件归属不变；若消息里的 [图片: id] 被移除，级联删该附件
```

---

## 7. 验证方式

| 层 | 验证 |
|---|---|
| 单测 | `test_attachment_store.py`：register（blob 落盘 + 记录可查）/ get / list_by_session / delete（记录+文件双清）/ delete_by_session / 幂等重复删 |
| 级联 | 复用 `test_session_delete.py` 风格：删消息 → 附件消失；删会话 → `_upload/` 无残留；`moofile_base UI` 能看到 `db_attachments_*` 库 |
| 接口 | `POST /api/attachments` 上传 → `GET /{file_id}` 返回图片（浏览器可开）→ 跨会话访问返回 403 |
| 前端 | 新建会话 → 触发 `browser_screenshot` → 消息中 `[图片: id]` 渲染为 `<img>`；刷新页面图片仍在；删消息/会话后图片消失 |
| token | 上下文 JSON 中确认图片仅以 `[图片: id]` 存在，无 base64 |

---

## 8. 实施顺序（建议）

1. `utils/attachment_store.py` + 单测
2. `api/attachments.py` + `app.py` 挂载 + 接口测试
3. `browser_screenshot` 接入（改落盘 + 注册 + 返回 file_id）
4. `api/context.py` / `session_context.py` 级联删除
5. 前端 `MarkdownMessage.vue` + `ChatPage.vue` 渲染
6. 全链路 e2e + 提交推送
