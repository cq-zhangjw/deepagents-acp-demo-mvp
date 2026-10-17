# 侧栏会话平铺 + 类型过滤 + 层级分组方案

日期：2026-10-17
状态：已确认，实施中（用户选 B：下线固定三区，保留自定义子分组）

## 背景与动机

原侧栏会话列表为「类别区（普通会话 / 工作任务 / 定时任务）→ 自定义分组 → 会话」三层嵌套。
会话变多时拥挤，且三类固定分区占用大量纵向空间（此前已有「会话变多挤压上方空间」反馈）。

用户决策（2026-10-17 确认）：
- **下线固定三个类别分区**，全部会话平铺。
- 每个会话以**类型徽标**标识（聊天 / 任务 / 定时）。
- 追加**类型过滤**：全部 / 普通会话 / 工作任务 / 定时任务。
- **保留一个顶层「所有会话」组**，组内可新建子分组（二级）管理会话；未分组会话直接在组内散落。
- 定时任务的面板入口（侧栏「定时任务」按钮 → 右侧面板）与定时任务的启用/禁用交互不变。

## 目标结构

```
侧栏
├─ 新建会话（按钮 → 弹窗选聊天/任务）
├─ 定时任务（按钮 → 右侧定时任务面板）
├─ Agent 管理
├─ 搜索会话（关键词过滤）
├─ 类型过滤：全部 | 聊天 | 任务 | 定时     ← 新增
└─ 所有会话（顶层组，可折叠，可新建子分组）
   ├─ （未分组会话直接平铺，散落）
   └─ 子分组（用户自定义，可折叠/重命名/删除/右键菜单）
      └─ 组内会话
```

过滤与搜索叠加：`关键词过滤 → 类型过滤`，计数与展示均以过滤后集合为准；过滤后为空的子分组隐藏。

## 数据与接口改动

### 后端 `api/groups.py`
- `KINDS = ("chat", "task", "timer")` → 追加 `"all"`：
  `KINDS = ("chat", "task", "timer", "all")`
- 新建分组前端一律传 `kind="all"`（顶层组下统一管理）；既有分组 `kind` 字段（chat/task/timer）保留兼容，展示层不再按 kind 分区。
- 其余接口（list/create/rename/delete、会话 `groupId` 移动逻辑）不变。

### 前端 `web/src/pages/ChatPage.vue`
- 删除：`chatConversations / taskConversations / timerConversations / normalConversations / categories` 五个 computed。
- 新增：
  - `filterMode = ref<'all' | 'chat' | 'task' | 'timer'>('all')`
  - `modeFilteredConversations` computed（`filteredConversations` 再按 `filterMode` 过滤）
  - `allGroups()`：全部子分组（不再按 kind 过滤，按 createdAt 排序）
  - `convsInGroupAll(gid)`：基于 `modeFilteredConversations` 按 `groupId` 过滤
  - 顶层组折叠 key：`'__all'`
- 改造：
  - `openCreateGroup()` 去 kind 参数，`groupModalKind = 'all'`
  - 右键「移动到分组」：列出 `allGroups()` + 「未分组」选项（不再按 `ctxConvKind` 过滤；`ctxConvKind` 删除）
  - 会话卡片统一带类型徽标（聊天/任务/定时），定时会话保留启用/禁用态样式
- 模板：
  - 搜索框下新增类型过滤条（4 个标签按钮：全部 / 聊天 / 任务 / 定时）
  - 会话区整体替换为「所有会话」组模板：组头（折叠 + 新建子分组按钮）→ 未分组平铺 → 子分组列表
  - 删除确认弹窗内分组会话计数改用 `convsInGroupAll`
- CSS：`.sidebar-filter`（过滤条）、沿用现有 conversation-group/sub-group 样式。

### i18n（zh / en / ja）
- 新增：`groups.all`（所有会话 / All conversations / すべての会話）、`filter.all`（全部 / All / すべて）等过滤文案；`mode.chat / mode.task / groups.timer` 复用现有文案。

## 影响评估（对现有功能）

| 功能 | 影响 |
|---|---|
| 会话模式（chat/task）分离 | 无影响：徽标与新建弹窗逻辑不变 |
| 定时任务面板 | 无影响：侧栏「定时任务」按钮入口不变 |
| 定时任务启用/禁用 | 无影响：平铺视图保留禁用态样式与交互 |
| 自定义分组（创建/重命名/删除/移动） | 语义调整：不再按类别建组，统一在「所有会话」下；既有 kind 数据兼容 |
| 搜索会话 | 无影响：类型过滤在其后叠加 |
| 右键菜单 | 「移动到分组」改为列出全部分组（更通用） |
| 数据 | 无迁移需求；`groupId` 引用不变；`kind='all'` 为新增合法值 |

## 验证方案

1. `npm run build` 通过。
2. Playwright（msedge headless, 1400x900, http://127.0.0.1:8000/）：
   - 启动默认新建聊天会话，徽标「聊天」，入「所有会话」平铺区。
   - 新建任务会话 → 徽标「任务」，同区平铺。
   - 过滤「定时」→ 列表仅剩定时会话（含空态隐藏）。
   - 过滤「聊天」→ 仅聊天会话；子分组为空则隐藏。
   - 新建子分组 → 组头出现；右键会话「移动到分组」→ 列出全部分组。
   - 截图人工确认布局无挤压。
3. 提交推送。

## 回滚

改动集中在 ChatPage.vue 模板/script 与 groups.py 一行 KINDS；git revert 即可完整回退。旧分组 kind 数据无迁移，无破坏性操作。
