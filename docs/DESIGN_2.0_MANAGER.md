# DeepAgents 2.0 · 技能 / 工具（MCP）/ Agent 管理面板设计

> 状态：已实施（2026-10-09，含后续调整：面板移除“新建技能/新建工具”表单，改为内置 skill-writer / tool-writer 技能由 AI 对话生成；Agent 迁入 `.deepagents/agents/` 目录（`*.agent.md` + `agents.json`）；工具面板顶部展示内置工具一览；技能编辑弹窗改为左右布局（左编辑右预览）；输入框 `@`/`/` 快捷动作已实现）
> 适用范围：`E:\my_projects\deepagents_acp`（Windows / FastAPI 网关 + ACP 子进程 + Vue3 前端）
> 关联：DESIGN_ZH.md（系统总设计）、UI_DESIGN.md（界面设计）

---

## 1. 背景与目标

当前 `acp_agent.py` 每次会话以固定配置装配 Agent：

- 模型：`utils/model_util.py` 从 `.env` 全局读取（`ENDPOINT/MODEL_NAME/TEMPERATURE...`）
- 工具：`create_deep_agent(tools=None)` —— 无任何外部工具，仅有内置文件/Shell 后端
- 技能：未启用（deepagents 库原生支持 Anthropic Agent Skills，未接线）
- 前端右侧面板（`right-panel`，300px 三列布局）仅有 `skills/tools/settings` 三个占位 tab（“该模块即将支持”）

**本设计目标**：

1. **技能面板**：通过本地 `.deepagents/skills/` 文件夹管理技能（SKILL.md 标准格式），支持启停、删除、编辑。
2. **工具面板（MCP）**：通过本地 `.deepagents/mcp_servers/` 文件夹管理 MCP 服务器配置，支持启停、删除、试连校验。
3. **Agent 管理**：由全局 `.deepagents/agents.json` 单文件管控 Agent（模型覆盖、系统提示词、关联的技能与工具），支持增删改查与“当前使用 Agent”选择。
4. **内置生成技能**：`.deepagents/skills/skill-writer`（生成符合规范的 SKILL.md）与 `tool-writer`（生成符合 stdio 规范的 server.json）随仓库内置，新建技能/工具通过对话让 AI 按规范生成，**不再提供表单式新建**。
5. **装配生效**：面板只负责**管理配置**（写 `.deepagents/` 文件）；acp_agent.py 如何按 Agent 配置装配模型/技能/MCP 工具，**由用户后续自行调整**，不在本设计实施范围。

**不做**（本轮范围外）：会话管理迁移 moofile（2.0 需求 5，另行设计）；共享屏幕（已完成）；语音通话（已完成）。

---

## 2. 目录结构（回答：是否统一放到 `.deepagents`？）

**结论：是。** 所有“可被面板管理的本地资源”统一收敛到项目根目录的 `.deepagents/`：

```
deepagents_acp/
├── .deepagents/                  # 管理根（git 提交，见 2.2）
│   ├── skills/                   # ── 技能库（文件夹管理）──
│   │   └── <skill-name>/         #    技能名：小写字母/数字/连字符
│   │       ├── SKILL.md          #    frontmatter(name/description) + 指令正文
│   │       ├── .disabled         #    存在 = 已停用（启停开关切换该文件）
│   │       └── helper.py / 素材   #    可选辅助文件（随技能一起分发）
│   ├── mcp_servers/              # ── MCP 服务器配置（文件夹管理）──
│   │   └── <server-name>/
│   │       ├── server.json       #    {"command","args","env","cwd","description"}
│   │       ├── .disabled         #    存在 = 已停用
│   │       └── (可选本地实现文件)
│   └── agents/                   # ── Agent 定义（专属目录）──
│       ├── agents.json           #    关联配置：{ <agent-name>: AgentDef }，见 3.3
│       └── <name>.agent.md       #    Agent 定义文件（markdown，frontmatter + 正文）
├── app.py / acp_agent.py / web/  # 现有代码
└── docs/
```

### 2.1 为什么用 `.deepagents/`

| 候选方案 | 评估 |
|---|---|
| 根目录散放 `skills/ mcp_servers/` + 单文件 `agents.json` | 与 `models/ uploads/ third_party/` 并列，结构平摊、根目录变乱；资源命名前缀无法体现归属 |
| **`.deepagents/` 统一目录** | 单一入口、一眼识别归属；与技能标准目录形态一致；整体复制/备份/忽略都方便；隐藏目录不影响根目录观感 |
| 数据库/单文件（sqlite/json 清单） | 用户明确要求“通过本地 skills 和 mcp_servers 文件夹管理”——文件即配置，可直接编辑、diff、git 管理 |

### 2.2 git 策略

- `.deepagents/` **纳入版本控制**（技能、MCP 配置、Agent 定义是可分享资产）。
- 例外：`server.json` 中的 `env` 可能含密钥——文档约定 `env` 字段支持 `${ENV_NAME}` 占位引用 `.env`，**禁止把明文密钥写入 server.json**（详见 3.2 安全）。
- `.deepagents/` 内无 node_modules 类依赖目录，技能辅助文件随目录提交。

### 2.3 命名与路径安全（统一规则）

- 目录名（skill/server/agent 名称）白名单：`^[a-z0-9][a-z0-9-]{0,63}$`（小写字母、数字、连字符）。
- 后端所有写/删操作基于白名单校验后的名称拼接路径，**杜绝 `../`、`\`、空名、`.`/`..` 路径穿越**。
- 删除操作不可逆 → 前端必须二次确认（`NPopconfirm`），后端 DELETE 仅允许删除 `.deepagents` 白名单目录。

---

## 3. 数据模型

### 3.1 技能（Skill）

沿用 Anthropic Agent Skills / 豆包技能标准——目录 + `SKILL.md`（YAML frontmatter）：

```markdown
---
name: web-research
description: Structured approach to conducting thorough web research
license: MIT
---

# Web Research Skill
## When to Use
...
```

- **识别**：扫描 `.deepagents/skills/*/SKILL.md`；`name` 与目录名一致（不一致时以目录名为准并警告）。
- **启停**：目录内 `.disabled` 文件存在 = 停用。启停开关 = 创建/删除该文件。
- **删除**：删整个技能目录。
- **编辑**：前端弹窗（正文 markdown 编辑）→ 覆写 `SKILL.md`。
- **新建（不提供表单）**：由内置 `skill-writer` 技能驱动——用户对话中描述需求，AI 按规范生成 `SKILL.md` 写入 `.deepagents/skills/<name>/SKILL.md`；面板列表即时刷新可见。

### 3.2 工具 / MCP 服务器（MCPServer）

**内置工具一览**：每个 deep agent 默认获得 `ls / read_file / write_file / edit_file / glob / grep / execute / task` 8 个内置工具（后端 `GET /api/manage/builtin-tools` 只读返回）。工具面板顶部固定展示这些内置工具 chips，下方才是可管理的 MCP 服务器列表。

每个 MCP server 一个目录，核心是 `server.json`（MCP stdio 启动规范）：

```json
{
  "name": "filesystem-mcp",
  "description": "本地文件系统 MCP 工具",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "E:\\workspace"],
  "cwd": null,
  "env": {
    "API_TOKEN": "${MY_TOKEN}"
  }
}
```

- **识别**：扫描 `.deepagents/mcp_servers/*/server.json`。
- **启停**：`.disabled` 标记文件（同技能）。
- **删除**：删目录（含配置与本地实现文件）。
- **试连**：面板按钮 → 后端用 `mcp` SDK `stdio_client` 启动 → `initialize + list_tools` → 断开，返回可用工具清单与错误。
- **新建（不提供表单）**：由内置 `tool-writer` 技能驱动——用户对话中描述需求，AI 按 stdio 规范生成 `server.json` 写入 `.deepagents/mcp_servers/<name>/server.json`；后端 POST 时自动试连并把 probe 结果带回前端。
- **安全约定**：
  - `env` 中 `"${NAME}"` 形式从进程环境（`.env` 已 `load_dotenv`）解析；**明文密钥写入 server.json 时 UI 给出警告**（仅提示，不强制）。
  - MCP `command` 可执行任意程序 → 面板属本地自用工具，UI 文案提示“仅运行可信命令”。

### 3.3 Agent 定义（`.deepagents/agents/` 目录）

Agent 拥有专属目录 `.deepagents/agents/`：
- **每个 Agent 一个 `<name>.agent.md` 文件**（markdown，frontmatter 含 name/description，正文为 Agent 行为/提示词定义，格式由用户后续自行约定）；
- **`agents.json` 放在该目录下**，只记录关联配置：对象映射，key 为 agent 名，value 为 AgentDef（model / system_prompt / skills / tools / enabled / description）。

面板的 Agent 增删改 = 同步操作 `<name>.agent.md`（创建/删除/可选正文覆写）+ `agents.json` 关联配置（原子写 + `.bak` 备份）。首次加载时自动把旧版根目录 `agents.json` 迁移到新结构。

```json
{
  "default": {
    "description": "默认 Agent（保持现状行为）",
    "model": null,
    "system_prompt": null,
    "skills": ["web-research"],
    "tools": ["filesystem-mcp"],
    "enabled": true
  },
  "coder": {
    "description": "代码专家 Agent",
    "model": {
      "provider": "openai",
      "model_name": "qwen3.8-9b",
      "base_url": "http://192.168.3.28:8088",
      "api_key": "-",
      "temperature": 0.2,
      "max_tokens": 10240
    },
    "system_prompt": "可选；缺省时使用 acp_agent.py 默认提示词 + AGENTS.md",
    "skills": ["web-research"],
    "tools": ["filesystem-mcp"],
    "enabled": true
  }
}
```

- **字段可省略**：省略 `model` → 装配侧用 `.env` 全局模型（保持现状）；省略 `skills/tools` → 面板侧不做关联（装配侧不加载对应资源）；省略 `description/system_prompt/enabled` → 分别默认空 / 默认提示词 / true。
- **enabled**：Agent 自身的启停开关，存在 json 内（与 skills/tools 的 `.disabled` 文件不同——agents 是单文件管控，无需额外标记文件）。
- **关联校验**：`skills`/`tools` 只接受“目录存在且未停用”的名称，加载时过滤并记录警告（不阻塞会话）。
- **新增/编辑**：前端表单（名称即 key，编辑态只读）+ 技能/工具多选（来自 3.1/3.2 列表，含停用项标灰）；创建时后端自动生成 `<name>.agent.md` 模板（frontmatter + 占位正文），正文可编辑后覆写。
- **删除**：删除 `<name>.agent.md` 并从 agents.json 移除该 key；删除当前正在使用的 Agent 时 UI 阻止并提示切换。

---

## 4. 后端 API（FastAPI 子路由 `/api/manage`）

新增 `api/manage/manager.py`（挂载于 `app.py`，前缀 `/api/manage`，与现有 `/api/chat_voice` 同模式）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/manage/skills` | 技能列表：`[{name, description, enabled, path, absolute_path, updated_at}]` |
| GET | `/api/manage/skills/{name}` | 技能 SKILL.md 正文（编辑弹窗回填） |
| POST | `/api/manage/skills/{name}/toggle` | 切换启停（创建/删除 `.disabled`） |
| POST | `/api/manage/skills` | 新建技能（name/description/content）→ 写 SKILL.md |
| PUT | `/api/manage/skills/{name}` | 编辑技能正文/描述 |
| DELETE | `/api/manage/skills/{name}` | 删除技能目录 |
| GET | `/api/manage/tools` | MCP server 列表：`[{name, description, command, enabled, tools_count?}]` |
| POST | `/api/manage/tools/{name}/toggle` | 切换启停 |
| POST | `/api/manage/tools` | 新建 MCP 配置（写 server.json）→ 自动试连 |
| POST | `/api/manage/tools/{name}/test` | 试连：启动→list_tools→返回工具名/错误 |
| PUT | `/api/manage/tools/{name}` | 编辑配置 |
| DELETE | `/api/manage/tools/{name}` | 删除 MCP 配置目录 |
| GET | `/api/manage/agents` | Agent 列表：`[{name, description, enabled, skills, tools, model, file, body}]`（扫描 `agents/*.agent.md` 合并 agents.json） |
| GET | `/api/manage/agents/{name}` | Agent 详情（完整 AgentDef + body，供编辑表单回填） |
| POST | `/api/manage/agents` | 新建 Agent（name + AgentDef）→ 写 `<name>.agent.md` + agents.json |
| PUT | `/api/manage/agents/{name}` | 编辑 Agent（覆写该 key 的 AgentDef；body 非空时覆写 .agent.md） |
| DELETE | `/api/manage/agents/{name}` | 删除 Agent（删 `<name>.agent.md` 并移除该 key） |
| GET | `/api/manage/agents/{name}/validate` | 预检：关联的技能/工具是否存在且已启用（不校验模型连通性） |
| GET | `/api/manage/builtin-tools` | 内置工具一览（只读）：ls/read_file/write_file/edit_file/glob/grep/execute/task |
| GET | `/api/manage/files?q=` | 项目根顶层条目（@ 快捷动作）：`[{name, path(绝对), type: dir|file}]`，q 前缀过滤 |

**通用约定**：
- 所有名称入参先过白名单校验（2.3），非法返回 400。
- 写文件用 UTF-8；编辑是**整文件覆写**（保持 json/md 可读格式）。
- 删除用 `shutil.rmtree`；失败（目录被占用）返回 409 + 明确提示。
- 响应统一 `{ok: true, data} | {ok: false, error}`。

---

## 5. 装配机制（说明与边界）

> **实施范围声明**：本文档实施部分**仅覆盖管理面板**（`.deepagents/` 文件读写 + 管理 API + 前端面板）。acp_agent.py 如何按 Agent 装配模型/技能/MCP 工具，**用户后续自行调整**，本设计仅给出装配所需的数据契约（agents.json / SKILL.md / server.json 的格式与字段），确保用户调整装配逻辑时无需改动面板侧协议。

### 5.1 数据契约（面板产出，装配侧消费）

- `.deepagents/skills/<name>/SKILL.md`：标准技能格式（frontmatter + markdown 正文），装配侧可用 deepagents `SkillsMiddleware(sources=[...])` 直接加载（deepagents 0.7.19 已验证支持 `skills=` 参数）。
- `.deepagents/mcp_servers/<name>/server.json`：MCP stdio 启动参数（command/args/cwd/env），装配侧可用官方 `mcp` SDK `stdio_client` 启动并 `list_tools()` 收集工具。
- `.deepagents/agents.json`：Agent 全局定义（对象映射，见 3.3），装配侧据此覆盖模型 / 提示词 / 关联技能与工具。

### 5.2 面板侧与装配侧的边界

| 职责 | 归属 |
|---|---|
| 扫描/列出/启停/删除/新建/编辑 `.deepagents/*` 资源 | 面板（`/api/manage` + 前端）——本次实施 |
| 按 agent.json 构造模型（init_chat_model 覆盖） | 装配侧（用户后续） |
| MCP 工具加载与注入 `create_deep_agent(tools=...)` | 装配侧（用户后续） |
| SkillsMiddleware 注入 `create_deep_agent(skills=...)` | 装配侧（用户后续） |
| 会话发起时携带当前 Agent 名 | 前端发送消息时携带（本次实施，字段见 6.4） |

---

## 6. 前端设计（右侧面板实装）

### 6.1 面板结构（保留现状入口模式）

**沿用现状交互，不引入内部 tab 栏**：面板内不显示 tab 切换标签，**点哪个入口就显示哪部分内容**——`openRightPanel(tab)` 打开面板并直接渲染对应内容区；右上角关闭按钮保留。

```
顶部操作区入口（现有技能/工具图标 + 新增 Agent 图标）
  └─ 点击 → openRightPanel('skills'|'tools'|'agents'|'settings')
       → right-panel 显示对应内容（面板标题 = 入口名称）

[right-panel]
├── header: 当前入口标题 + 关闭按钮（保留现有）
└── body:
    ├── skills 入口 → 技能管理（见 6.2）
    ├── tools 入口  → MCP 工具管理（见 6.3）
    ├── agents 入口 → Agent 管理（见 6.4）★新增
    └── settings 入口 → 保留占位（后续：全局设置项）
```

- `rightPanelTab` 类型扩展：`'skills' | 'tools' | 'agents' | 'settings'`；面板模板由 `v-if` 按当前 tab 渲染对应内容（结构同现有 1571-1589 行，仅把占位替换为真实组件）。
- 新增“Agent”入口图标（`RocketOutline` 或 `Person` 风格，放技能/工具图标旁）。

### 6.2 技能 tab

- 列表卡片：技能名、描述（截断）、启停开关（`NSwitch`）、“编辑”“删除”（`NPopconfirm` 二次确认）。
- 顶部“新建技能”按钮 → 弹窗表单：名称（只读于编辑态）、描述、正文（`NInput type=textarea` 或 monospace 编辑，保留 markdown 原始文本）。
- 停用技能：卡片置灰 + 开关关闭；列表顶部排序“启用在前”。
- 空状态：引导文案 + 新建按钮。

### 6.3 工具（MCP）tab

- 列表卡片：server 名、描述、command 摘要（monospace）、启停开关、试连按钮（`NButton` 显示工具数或错误）、编辑、删除。
- 新建/编辑表单：name / description / command / args（`NInput` 换行分隔）/ cwd / env（`key=value` 多行）。
- 保存后自动试连：成功显示 `NAlert`（成功 + 工具数），失败显示错误信息（红色）。
- `env` 含明文值时警告提示（`${VAR}` 形式不警告）。

### 6.4 Agent tab

- 列表卡片：Agent 名、描述、关联技能/工具 chips（`NTag`）、启停开关、编辑、删除。
- **“设为当前”按钮**（Radio/高亮）：当前 Agent 存 `localStorage.current_agent`，发送消息时随请求携带；高亮“正在使用”。
- 新建/编辑表单：
  - 基础：name（新建可填，编辑只读）、description
  - 模型：provider / model_name / base_url / api_key（password 输入）/ temperature / max_tokens（留空 = 使用 .env 全局）
  - 提示词：system_prompt 多行（留空 = 默认）
  - 关联：技能多选（`NSelect multiple`，来自 /api/manage/skills）、工具多选（来自 /api/manage/tools）
- 删除当前使用中的 Agent：UI 阻止（提示先切换）。

### 6.5 i18n

zh/ja/en 三语（`web/src/locales/*.ts` 新增 `manager` section）：skills/tools/agents/settings、新建/编辑/删除/启停/试连/设为当前、各类提示。

---

## 7. 与现有代码的衔接清单（实施清单）

| 文件 | 改动 |
|---|---|
| `.deepagents/skills\|mcp_servers/` 骨架 + `.deepagents/agents.json` | 新建目录骨架 + agents.json 含 1 个示例（default Agent） |
| `api/manage/manager.py` | 新增：4. 的 API 全部实现 + 路径安全校验 + 文件读写 |
| `app.py` | 仅挂载 `/api/manage` 子路由（**不涉及 ACP 子进程参数改动**） |
| `requirements.txt` | 追加 `mcp`（英文注释；仅用于 tools 试连校验） |
| `web/src/pages/ChatPage.vue` | right-panel 内容实装（skills/tools/agents/settings 按入口渲染）+ Agent 入口图标 + 当前 Agent 状态 |
| `web/src/api/*` | 新增 manager API 封装 |
| `web/src/locales/*.ts` | manager section 三语 |
| `docs/DESIGN_ZH.md` / `README.md` | 同步：目录结构、/api/manage、面板功能 |
| `acp_agent.py` / `utils/model_util.py` / `utils/mcp_loader.py` | **不改动**（装配逻辑由用户后续自行调整） |

---

## 8. 风险与边界

1. **MCP command RCE**：面板允许任意命令定义 → 仅本地自用；UI 明示“仅运行可信命令”；`.deepagents` 提交到 git 时配置随仓库传播，使用者自查。
2. **删除不可逆**：前端二次确认 + 后端白名单 + 失败 409。
3. **试连失败提示**：MCP 配置保存/试连失败（命令不存在、端口不通等）→ 面板明确提示错误信息；Agent 关联了已停用/不存在的技能或工具 → `/validate` 预检报告，不静默忽略。
4. **并发写**：技能/工具为目录级写（无全局冲突）；**agents.json 为单文件，所有 Agent 增删改共用**——采用进程内写锁（asyncio.Lock / threading.Lock）+ 写前备份 `.bak` + 原子替换（tempfile+replace）；读取侧容忍短暂不一致（下次启动生效）。
5. **模型连通性**：agent.json 配了无效模型 → 会话内模型调用报错（与现状一致，前端显示错误消息）。
6. **范围约束**：不引入数据库、不迁移 moofile 会话（2.0 需求 5 另文设计）。

---

## 9. 待确认决策点

| # | 决策 | 建议 | 备选 |
|---|---|---|---|
| 1 | 统一目录 `.deepagents/`（skills/ mcp_servers/ 文件夹 + agents.json 单文件） | ✅ 采用 | 根目录散放 / 每 agent 一目录 |
| 2 | 启停机制 `.disabled` 标记文件 | ✅ 采用（不改 SKILL.md/json 结构即可切换） | frontmatter / json 字段 |
| 3 | MCP 工具命名 / 连接生命周期 | 装配侧决策（用户后续调整时自定） | — |
| 4 | Agent 当前值存 localStorage + 随消息携带 | ✅ 采用（仅前端记录与展示） | 后端 session 级记忆 |
| 5 | settings 入口 | 本轮保留占位 | 扩展全局设置 |

