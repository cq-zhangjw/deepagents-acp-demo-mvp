# 会话分组管理 —— 实施方案（详细）

> 状态：**方案存档（暂不实施）**
> 关联方案：`docs/chat-task-mode-plan.md`（会话分为 聊天/任务/定时任务 三类，本方案在每类下再支持用户自定义分组）
> 适用版本：当前 main
> 目标：在「普通会话（聊天）」「工作任务」「定时任务」三类下，用户可创建分组（文件夹）管理会话——建组、重命名、删除、把会话移入/移出分组。

---

## 1. 需求与目标

会话当前只有两类内建分组（`conversation-group-label`：普通会话 / 定时任务），列表平铺、不可自定义。本方案：

1. 会话划分为三类（与 chat/task 模式方案联动）：
   - **普通会话**：`mode=chat`（或历史缺省、无任务的普通会话）
   - **工作任务**：`mode=task`（任务模式会话）
   - **定时任务**：id 以 `timer-` 开头的会话
2. 每一类下，用户可创建多个**自定义分组**，把会话移入分组；未分组会话显示在该类"全部"区。
3. 分组支持：创建 / 重命名 / 删除 / 折叠；会话支持：移入分组 / 移出分组（右键菜单）。

**不做（一期）**：分组手动排序/拖拽（二期）；分组级独立滚动（保持类别级滚动，见 §5）。

---

## 2. 数据模型

### 2.1 会话记录（`api/history.py` MooFile `db_history`）

现有宽松 dict 增加字段：

```jsonc
{
  "id": "...",
  "title": "...",
  "updatedAt": 1234567890,
  "mode": "chat" | "task",          // 关联方案字段；缺省 task
  "groupId": null | "g_xxxx",       // 新增：所属分组；null = 未分组
  "...": "..."
}
```

### 2.2 分组记录（新增 MooFile 库 `db_groups`，与 `db_history` 同级 `db/db_groups_*`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `g_<hash8>`，会话 `groupId` 引用 |
| `name` | string | 分组名（≤30 字） |
| `kind` | `"chat" \| "task" \| "timer"` | 所属类别（分组只出现在对应类别下） |
| `createdAt` | number | 排序依据（旧→新；不做手动排序） |

**与 `api/history.py` 同构实现**：`_HISTORY_DB` 命名模式扩展为 `GROUPS_DB`，`_ensure()` 首跑建库。

---

## 3. 后端 API（新增 `api/groups.py`，挂到主 router）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/groups` | 全部分组（含各 `kind`） |
| POST | `/api/groups` | 创建 `{name, kind}` → 返回分组记录 |
| PUT | `/api/groups/{id}` | 重命名 `{name}` |
| DELETE | `/api/groups/{id}` | 删除分组；组内会话 `groupId` 置 null（**不删会话**） |

**移动会话**：复用现有 `PUT /api/history/{id}`（upsert 整条会话记录）——前端把会话的 `groupId` 一起提交，无需新端点。

**删除分组的会话回写**：DELETE 分组时后端批量把该 `groupId` 的会话 `groupId` 置 null（遍历 `list_history()` 匹配更新）。

---

## 4. 前端 UI（`ChatPage.vue`）

### 4.1 三类视图（替换现有两组渲染）

顶部三个类别（替代现有「普通会话」「定时任务」两个 label），每类结构：

```
▼ 普通会话 12
  ├─ ＋ 新建分组            ← 类别 label 行右侧小按钮
  ├─ 分组A ▾ (5)           ← 自定义分组，可折叠
  │    ├─ 会话1
  │    └─ 会话2
  ├─ 分组B ▾ (2)
  └─ 未分组 (5)            ← 内置"全部"，不可删除
       ├─ 会话x
       └─ ...
```

对应类别数据（computed，扩展现有 184-188 行）：

```ts
const chatConversations = computed(() =>
  filteredConversations.value.filter((c) => !String(c.id).startsWith('timer-') && (c.mode ?? 'task') === 'chat'))
const taskConversations = computed(() =>
  filteredConversations.value.filter((c) => !String(c.id).startsWith('timer-') && (c.mode ?? 'task') === 'task'))
const timerConversations = computed(() =>
  filteredConversations.value.filter((c) => String(c.id).startsWith('timer-')))
```

### 4.2 分组操作

| 操作 | 入口 | 行为 |
| --- | --- | --- |
| 新建分组 | 类别 label 行「＋」 | 弹出输入框（NDialog/NDropdown），选 kind 自动为该类别 |
| 重命名 | 分组 label 右键 | 复用现有 `openRenameMenu` 模式（`renameMenu` 弹层），改 `name` |
| 删除分组 | 分组 label 右键 | 确认弹窗（复用 `delete-confirm-body` 样式），提示"组内 N 个会话将移出分组，会话不删除" |
| 折叠分组 | 点击分组 label | 本地状态 `collapsedGroups: Set<groupId>`（同 `normalCollapsed` 机制） |
| 会话移入分组 | 会话项右键菜单扩展 | 「移动到分组 ▸ 分组A / 分组B / 未分组」 |
| 会话移出分组 | 同上 | 选「未分组」→ `groupId=null` |

### 4.3 状态与模板

- 新增 `const groups = ref<Group[]>([])`，`loadGroups()` 随 `loadConversations` 一起拉取（`/api/groups`）；
- 会话 `Conversation` 类型加 `groupId?: string | null`、`mode?: 'chat' | 'task'`；
- 右键菜单：现有 `renameMenu`（600 行）扩展为 `ctxMenu = { id, x, y, kind }`，提供「重命名 / 移动到分组 ▸… / 删除会话」三项；
- 移动操作：改本地会话 `groupId` 后 `PUT /api/history/{id}`（复用 404 行现有保存路径）。

### 4.4 搜索时行为

`conversationSearch` 过滤（176 行 `filteredConversations`）不变——搜索时**扁平显示匹配会话**（忽略分组层级，便于查找），清空搜索恢复分组视图。实现：`filteredConversations.length && search 非空` → 用现有平铺列表渲染分支。

---

## 5. 滚动取舍（与现有"组内滚动"的关系）

- 现有滚动：类别容器 `.conversation-group-list` 内部滚动、label 固定（已实现并验证）；
- 本方案：**滚动层级保持在"类别"级**——类别容器滚动，自定义分组是折叠单元（展开时组内会话全部渲染在类别容器里，不单独滚动）；
- 理由：多个分组各自滚动会互相挤压高度、滚动条嵌套体验差；
- 若后续需要"分组级滚动"（一个分组内容很多时只滚该组），二期在 `.conversation-group-list` 下再加一层分组容器实现。

---

## 6. 与现有机制的关系

1. **timer 引擎写会话记录**（`timers_engine.py` 326/357/385 行附近）：会话被定时任务刷新 `updatedAt/title` 时，**必须保留 `groupId`（和 `mode`）**——`put_history` 若整体替换会丢字段。落地：history 写入统一"读-改-写"（先 `get_history(cid)` 合并旧字段再 upsert），timer 引擎同理带出既有字段；
2. **chat/task 模式方案联动**：新建会话时确定 `mode` → 落在「普通会话」或「工作任务」类下；`mode` 锁定，分组跟随会话（会话跨类移动**不允许**——分组 `kind` 决定会话归属，移入分组时校验 kind 匹配）；
3. **删除会话**：现有 `DELETE /api/history/{id}` 不变，与分组无关；
4. **Default/定时任务面板**（右侧 `right-panel`）：不受影响。

---

## 7. 改动清单

| 文件 | 改动 |
| --- | --- |
| `api/history.py` | 写入改为"读-改-写"保留 `groupId/mode`；文档注释 |
| `api/groups.py`（新） | 分组 CRUD + 删除分组时回写会话 groupId=null |
| `app.py` | 挂载 groups router |
| `api/timers_engine.py` | 会话 upsert 保留 `groupId/mode` |
| `web/src/pages/ChatPage.vue` | 三类视图 + 分组创建/重命名/删除/折叠 + 右键"移动到分组" + 搜索扁平化 |
| `web/src/types.ts`（如存在） | `Conversation` 加 `mode/groupId`；`Group` 类型 |
| `web/src/locales/{zh,ja,en}.ts` | 分组相关文案（新建分组/重命名/删除/移动到分组/未分组等） |

---

## 8. 验证方案（实施时执行）

1. **后端**：POST 建组（三 kind）→ GET 返回；PUT 改名；DELETE 组 → 断言组消失、组内会话 `groupId=null` 且会话仍在；
2. **字段保留**：对带 `groupId/mode` 的会话跑 `put_history`（模拟 timer 引擎刷新）→ 断言字段不丢；
3. **端到端（Playwright）**：
   - 三类视图各显示正确（chat/task/timer 会话各归其位，旧会话缺省 task）；
   - 新建分组 → 出现于对应类别；右键会话 → 移动到分组 → 刷新后仍在；
   - 删除分组 → 会话回到未分组；
   - 搜索关键词 → 扁平匹配结果；清空 → 恢复分组视图；
   - 40 条会话注入 → 类别容器滚动正常、分组 label 固定。
4. **回归**：无分组数据时界面与现状一致（三类各一个 label + 平铺列表）。

---

## 9. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| timer 引擎覆盖丢字段 | history 写入改"读-改-写"；定时任务刷新后抽查 |
| 分组删除误删会话 | 删除仅置 groupId=null，确认弹窗明示"会话不删除" |
| 跨类移动混乱 | 移入时校验 kind；UI 只显示同类别分组 |
| 旧会话无 mode/groupId | 缺省 task / null，界面归位"工作任务/未分组"，零迁移 |

回滚：`api/groups.py` 移除 + 前端分组渲染分支回退（独立提交，可独立 revert）；`history.py` 读-改-写改动向后兼容，不影响旧数据。

---

## 10. 二期候选（不在本次范围）

- 分组手动排序 / 拖拽会话跨组（含跨类校验）；
- 分组级独立滚动；
- 基于分组的批量操作（整组删除/重命名/导出）。
