# 会话模式分离（Chat 聊天 / Agent 任务）

> 状态：**已实现**（2026-10-29）
> 本文档替代 v3.2 前的旧版模式分离方案：不再做分组、不再做多种模式，只保留「Chat 聊天」与「Agent 任务」两种会话模式。

## 背景与目标

- 普通聊天场景下，用户不需要子 Agent、技能、工具、知识库等入口，也不需要任务导向的 AGENTS.md 提示词。
- 目标：新建会话时由用户选择「Chat 聊天」或「Agent 任务」；Chat 会话为轻量纯聊天（无任何工具/技能/文件能力），Agent 任务会话保持现有完整工作区行为（默认 Default Agent）。

## 需求口径（用户确认）

1. `acp_agent.py build_agent` 新增 `agent_name == "Chat"` 分支：Chat 不装配 MCP 工具、子 Agent、技能。
2. 新建「ChatAgent」提示词：模仿豆包语气——总是很耐心、不罗嗦恰到好处、喜欢用 emoji 表达情感。
3. 新建会话时可选「Chat 聊天 / Agent 任务」两种模式；Chat 聊天模式不显示子 Agent、技能、工具、知识库入口。
4. Chat 模式 Agent 固定为 Chat，不可切换。
5. Agent 任务模式默认 Default Agent（当前行为，兼容）。

## 架构实现

### 链路（不改协议，只改参数）

```
前端 WebSocket /acp-ws?agent=<name>
  → app.py acp_bridge 读取 query，注入 AGENT_NAME 环境变量
  → acp_agent.py build_agent(agent_name) 按名字组装
      ├─ "Chat"  → tools=[] / subagents=None / skills=[] / StateBackend / chat.agent.md 人设
      └─ 其他    → 完整 DeepAgent（默认 default 与现状一致）
```

会话对象新增 `mode?: 'chat' | 'task'` 字段，随 `/api/history` 全量落库（MooFile 透传，后端无需改）。

### 后端（acp_agent.py）

- **build_agent**：
  - 修复非 Chat 分支缺失 `return agent` 的 bug（原先整个 if/elif 走完隐式返回 None，AgentServerACP 会在会话非首轮时拿不到 agent）。
  - Chat 分支：`chat_backend = StateBackend()`（虚拟内存后端，无文件/命令能力），`return agent`。
- **get_system_prompt("Chat")**：Chat 特判——**不继承任务导向的 `./AGENTS.md`**（避免 todo / 文件工具指令泄漏进纯聊天回复），只加载 `.deepagents/agents/chat.agent.md`；缺失时返回英文兜底人设。
- 新增 `.deepagents/agents/chat.agent.md`：豆包人设（耐心、不罗嗦恰到好处、emoji 表达情绪、像朋友、不编造、不调用工具、跟随用户语言）。

### 前端（web/src/pages/ChatPage.vue）

- `Conversation` 接口新增 `mode?: 'chat' | 'task'`（旧数据缺省视为 task）。
- `isChatMode` computed：当前会话 `mode === 'chat'`。
- **新建会话弹窗**：侧栏「新建会话」/移动端按钮 → `openNewConversation()` 弹出 NModal，两个选项（Chat 聊天 / Agent 任务）→ `createConversationWithMode(mode)`。
- **createConversation(mode)**：
  - `chat`：`currentAgentName = 'Chat'`（持久化）+ `resetSocketForAgentChange()`；复用同模式空会话。
  - `task`：`resetAgentToDefault()`（默认 Default，兼容现状）。
  - 进入画面自动新建走 `createConversation('task')`（**2026-10-29 晚改为不自动新建**，见下方「初始画面调整」）。
- **selectConversation** 按会话 mode 对账 agent：
  - Chat 会话 → `currentAgentName = 'Chat'`（若不同则重建 WS）。
  - task 会话 → 若当前是 'Chat' 则回 'default'（重建 WS）；task 会话之间保留各自的 agent 选择（现状兼容）。
- **工具条收敛**（`v-if="!isChatMode"` 包住 Agent 下拉 / 子 Agent / 技能 / 工具 / 知识库；Chat 模式只显示固定 Chat pill）。附件上传按钮两种模式均保留。
- **Agent 固定 Chat**：Chat 模式 `selectAgent` 直接 return；下拉不渲染，改为 disabled 的 Chat pill。
- 会话列表：Chat 会话标题旁显示「Chat 聊天」小标签。

## 行为矩阵

| 场景 | 行为 |
| --- | --- |
| 新建会话 | 弹窗选择 Chat 聊天 / Agent 任务 |
| Chat 会话 | agent=Chat；无工具/技能/子 Agent/知识库入口；Agent 固定 Chat 不可切换 |
| Agent 任务会话 | 默认 Default Agent；完整工作区（子 Agent/技能/工具/知识库/Agent 下拉） |
| Chat → task 切换 | agent 从 Chat 复位为 default，WS 重建 |
| task → Chat 切换 | agent 置为 Chat，WS 重建 |
| 历史旧会话（无 mode） | 按 task 处理，行为不变 |
| 侧栏会话列表 | 三类分组（可折叠）：Agent 会话（task 模式，置顶）、普通会话（chat 模式）、定时任务（timer-* 会话，常驻显示，空时提示「暂无定时任务」）；组不设最大高度，整列一个滚动条 |

## 验证记录（2026-10-29）

- 后端 WS 直连验证：`?agent=Chat` 回复符合豆包人设（自称豆包、emoji 😊、无任何工具调用）；`?agent=default` 正常流式回复（build_agent 修复生效）。
- 浏览器验收：新建会话弹窗正常；Chat 会话工具条收敛（仅附件+输入+发送+Chat pill）；task 会话工具条完整（Default/子Agent/技能/工具/知识库）；会话列表 Chat 标识正确；切换会话模式联动正确。
- 前端 `npm run build` 通过。

## 注意事项

- `.deepagents/agents/chat.agent.md` 文件名小写 `chat`，后端 `_load_agent_md("Chat")` 在 Windows NTFS（大小写不敏感）可命中；**跨平台（Linux）部署需统一大小写**（建议改文件名为 `Chat.agent.md` 或让 `_load_agent_md` 大小写不敏感）。
- `utils/model_util.py` 中 `print(f"Using model endpoint: ...")` 会打印到 agent 子进程 stdout，前端 `handleAcpMessage` 对非 JSON 行需容忍（当前已容错，历史任务验证过）。
- 定时任务引擎的 agent 选项来自 `agents.json`（不含 Chat），定时任务不会使用 Chat agent。

## 初始画面调整（2026-10-29）

1. **不再自动新建会话**：onMounted 移除 createConversation('task') 与重复空会话清理段；初始画面（无活动会话）展示会话模式选择。
2. **模式选择 tab**：初始画面顶部「Start a task」区替换为「Chat 聊天 / Agent 任务」两个 tab（initialMode ref，默认 task）。选中切换标题、描述与特性列表（复用 chatModeDesc/taskModeDesc/chatModeFeats/taskModeFeats 文案）。
3. **工具栏联动**：无活动会话时工具栏收敛状态跟随 initialMode（`toolbarChatMode` computed：Chat → 只留附件/发送；Agent → 完整工具栏）。selectAgent 在 Chat 模式下仍直接 return。
4. **垂直居中**：`workspace--initial` 类使 `timeline-wrap` 与 composer-wrap 一起在 header 下方垂直居中（输入框上移居中）；发送消息后新建会话、进入正常聊天布局，输入框回到底部。
5. **发送即建会话**：submitPrompt 开头 if (!currentConversation.value) createConversation(initialMode.value)——初始画面直接发送时按所选模式新建会话。
6. 空会话（有会话无消息）仍显示原「Start a task」占位欢迎区。
7. **AI 消息背景色**：.message--assistant .message-body 加白底 #fff + 边框 #e6ebf2 + 圆角 8px + padding 12px 14px，与用户浅蓝气泡（#eaf1ff）区分，增强边界感。

### 调整二：新建会话去弹窗 + header 固定顶部（2026-10-29）

1. **header 固定顶部**：原 `.workspace--initial { justify-content:center }` 会把整个 workspace（含 64px header）垂直居中，导致右上角语言/通话/扬声器等工具栏被挤到屏幕约 1/3 处。改为去掉该规则，`.workspace--initial .timeline-wrap { margin-top:auto }` + `.composer-wrap { margin-bottom:auto }`，header 保持顶部，仅内容区（模式选择 + 输入框）在剩余空间垂直居中。
2. **新建会话去弹窗**：删除 NModal 弹窗（`newConversationVisible` / `createConversationWithMode` / `.new-conversation-*` 样式 / `newConversationModeTitle`·`Desc` 文案）。`openNewConversation()` 改为直接 `activeConversationId = ''` 并清理会话态（contextOverride/messageWindowSize/attachments/errorText/conversationSearch）+ 关闭右侧面板与移动端侧栏，显示初始模式选择页；发送消息时按所选模式新建会话（原逻辑不变）。

### 调整三：模式偏好持久化（浏览器 localStorage，2026-10-29）

1. **存储键**：`chat_initial_mode`（值 `'chat' | 'task'`），纯浏览器 localStorage，无后端参与。
2. **读取**：`initialMode` ref 初始化时读取 localStorage（缺省 `'task'`），主页（初始模式选择页）默认选中已保存的模式。
3. **保存时机（最近一次为准）**：
   - 初始页点击「Chat 聊天 / Agent 任务」tab → watch(initialMode) 立即写入；
   - 从侧栏进入历史会话（非定时任务）→ 按该会话的 mode 同步 initialMode（watch 自动落盘），即"最近使用的模式"成为主页默认。
4. **无副作用**：selectConversation 更新 initialMode 不影响工具栏（toolbarChatMode 仅在无活动会话时取 initialMode）；watch 无 immediate，页面加载不产生写回。
