# Playwright 浏览器工具内置方案

> 状态：方案（评估完成，待实施）
> 关联：`browser_server` MCP（`.deepagents/tools/mcp_servers/browser_server/`）、deepagents_acp 网关
> 更新：2026-10-14

## 1. 背景与目标

- 网关启动弹窗已改为系统命令（`webbrowser` + `OPEN_BROWSER` 环境变量），不再依赖 Playwright。
- 浏览器能力目前以 **MCP 工具** 形式提供给 deepagents（AI 使用）：`browser_server` 是独立 stdio 子进程，用 sync Playwright 控制 Edge。
- **目标**：把浏览器能力稳定内置到 AI 使用流程中——**单实例常驻、AI 复用同一个浏览器、登录态可保持、生命周期与权限可控**，避免"每次任务反复启动浏览器"。

## 2. 现状盘点（已确认事实）

| 项 | 现状 | 说明 |
|---|---|---|
| 浏览器启动 | 懒初始化 | `go_to_url` 等工具内 `if playwright is None and browser is None: init_browser()`——**首次工具调用才启动，之后复用** ✅ 已满足"单实例常驻" |
| 进程形态 | 独立 stdio MCP 子进程 | 随 deepagents 会话启停；会话期间浏览器常驻 |
| 浏览器 | Edge（`channel="msedge"`） | 非 headless（`headless=False`，有可见窗口），`--test-type` 压制提示条、`chromium_sandbox=True` |
| Profile | `user_data_dir=None`（临时目录） | **登录态不保持**，每次会话重新登录 |
| 与用户日常浏览器隔离 | ✅ 隔离 | 独立临时 profile，不混用用户 Edge 数据 |
| 工具集 | go_to_url / get_element / wait_for_element 等 | 交互类工具已有基础，**缺"看"的能力**（截图、文本提取） |

## 3. 设计要点

### 3.1 单实例常驻（维持 + 加固）

- 维持懒初始化（首次调用启动、全局 `browser`/`playwright` 复用）。
- **加固**：工具调用内部捕获异常，浏览器实例失效时（如被用户关窗）自动重建（检测 `browser` 句柄异常 → 重新 `init_browser`），不让单次失败导致后续调用全部不可用。
- 可选：显式提供 `browser_open`（初始化/复位）与 `browser_close` 工具，方便 AI 主动管理生命周期。

### 3.2 登录态保持（关键增强）

- `user_data_dir` 从 `None` 改为固定目录：`.deepagents/tools/mcp_servers/browser_server/profile/`（或项目级 `browser_profile/`）。
- 效果：AI 登录一次（如 SharePoint/后台系统），后续会话/任务复用同一 Edge profile，无需重复登录。
- **风险**：单 profile 并发冲突 → 由"单实例"保证串行（见 3.5）。

### 3.3 工具集补齐（AI 真正可用的最小集）

| 工具 | 用途 | 优先级 |
|---|---|---|
| `browser_screenshot` | 截图回传（AI 视觉理解页面） | P0 |
| `browser_get_content` / `browser_get_text` | 提取 DOM 文本/结构化内容供 LLM 读取 | P0 |
| `browser_click` / `browser_fill` / `browser_select` | 点击/输入/下拉选择 | P0 |
| `browser_back` / `browser_refresh` / `browser_new_tab` / `browser_close_tab` | 导航与标签管理 | P1 |
| `browser_wait` | 等待元素/条件 | P1 |
| `browser_get_state` | 当前 URL/标题/标签列表 | P1 |
| `browser_execute_script` | JS 注入（**高风险**，需权限确认） | P2 |

### 3.4 与 deepagents 集成

- 维持 stdio MCP 注册（`agents.json` / `manifest.json`），随 deepagents 启动。
- 工具描述面向 LLM 写清楚（何时用、参数含义、返回结构）。
- 参数 schema 精确化：`utils/mcp_loader.py` 的类型映射修复已就绪（str/int/float/bool 透出），新工具按此规范声明。

### 3.5 并发与一致性

- 单实例单浏览器 → 同一 deepagents 会话内多个 AI 步骤**串行**操作同一页面，避免并发互踩。
- 若未来多任务并行：加工具级互斥锁（同一 context 上锁，任务队列化）。
- 跨 deepagents 进程：各自独立 MCP server 与浏览器（profile 目录建议按进程区分，或接受单写者）。

### 3.6 安全与权限

- 浏览器隔离：始终使用**独立 Edge profile**（不与用户日常浏览器混用）。
- 敏感操作权限：`execute_script`、表单提交、下载等操作接入 deepagents 的 `request_permission` 确认机制（人工放行）。
- 可见性：默认 `headless=False`（AI 操作可见，便于人监督）；可选 `headless=True`（后台运行）由环境变量控制。
- 数据边界：AI 能访问的页面即其会话内授权范围；profile 内登录态属于 AI 会话，不扩散到用户浏览器。

### 3.7 演进路径

- **P0（最小可用，建议先做）**：固定 `user_data_dir` + `browser_screenshot` + `browser_get_content` + 崩溃自愈。→ 此时 AI 已能"看"和"读"，可完成大部分页面自动化。
- **P1**：补齐交互/导航工具集 + schema 精确化 + 工具描述优化。
- **P2（可选）**：浏览器服务化（网关统一管理，deepagents 经 HTTP/WS 连接），跨会话复用登录态、支持多客户端查看同一页面；permission 集成。
- **P3（可选）**：headless 切换、多 profile、代理/下载策略。

### 3.8 风险与对策

| 风险 | 对策 |
|---|---|
| AI 误操作/误提交 | 权限确认机制（P2 接入 request_permission） |
| 登录态/页面数据泄露 | 独立 profile + 会话内授权边界 |
| 浏览器崩溃/被用户关闭 | 工具级 try/except + 自动重建 |
| 单实例阻塞 | 任务串行 + 超时控制（`GLOBAL_TIMEOUT`） |
| 页面反爬/检测 | 现有隐藏自动化特征参数保留（`--test-type`、AutomationControlled） |

## 4. 结论

- **可行，且已有雏形**：单实例懒加载已满足"不反复启动浏览器"的核心诉求。
- **最小必要改动**：① 固定 `user_data_dir`（登录态复用）；② 补 `browser_screenshot` + `browser_get_content`（AI 视觉/文本读取，否则工具集不可用）；③ 崩溃自愈。
- **建议路径**：先按 P0 落地并随 deepagents 使用验证，再按需推进 P1/P2，不急于服务化。
