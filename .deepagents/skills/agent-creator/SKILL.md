---
name: agent-creator
description: 交互式创建符合本仓库规范的 Agent。依次确认 Agent 名字、职责与使用时机、所需技能/工具/向量知识库，生成 <name>.agent.md 并注册到 agents.json。当用户说"创建一个 agent / 新建 agent / 帮我做个 XX agent / 帮我写个角色"时使用。
---

# Agent Creator

## 何时使用

用户表达以下意图时启用本技能：

- "创建一个 agent / 新建 agent / 增加一个 agent"
- "帮我做一个 XX 方向的 agent / 写个 XX 角色"
- "让 AI 扮演 XX 角色来处理 YY 任务"

**先收集完第 1 步的全部信息，再开始写文件。** 禁止在信息不全时凭猜想象写入。

## 流程

### 第 1 步 收集信息（逐个询问，缺一不可）

按顺序向用户确认以下 5 项，每项确认后再问下一项：

1. **Agent 名字**
   - 要求：英文 camelCase（如 `languageLearning`、`codeReviewer`），首字母小写；与后续文件名、agents.json 的 key 完全一致
   - 禁止：中文、空格、连字符、大写开头

2. **作用与使用时机**
   - 要求用户说清楚：这个 agent 负责什么、什么时候应该被选用
   - 这决定 description（一句话）和正文的角色定位

3. **技能选择**
   - 列出 `.deepagents/skills/` 下全部技能目录（排除 `.enabled` 系统目录），形如：
     `skill-creator`（生成符合规范的技能）、`tool-creator`（生成符合规范的工具）、`agent-creator`（当前技能）
   - 让用户多选；一个都不选则明确记录"不启用任何技能"

4. **工具选择**
   - 内置工具（固定集合，让用户多选）：
     `ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`、`delete`、`task`、`execute`
   - MCP 工具：读取 `.deepagents/tools/mcp_servers/<server>/manifest.json`，按服务列出工具名，如：
     `calc_server`：`calculate`（计算数学表达式）
     `knowledge_server`：`list_knowledge_bases` / `retrieve_knowledge` 等（知识库服务）
   - 让用户选择需要哪些服务的哪些工具；不选则 `mcp_tools` 为空数组

5. **向量知识库选择**
   - 请求 `GET /api/knowledge/databases?agent=default`（本项目网关），只展示 `type == "vector"` 的库（id、name、记录数、文档数）
   - **若返回空/请求失败**（知识库服务未启动）：明确告知用户"知识库服务当前不可用，本次跳过知识库关联"，继续后续步骤；不要报错中断
   - 让用户选择要关联的库；**只要选了任意一个**，就视为需要启用 `knowledge_server`（该 MCP 服务覆盖全部知识库，系统按服务启停）

### 第 2 步 生成 `.agent.md`

路径：`.deepagents/agents/<name>.agent.md`（唯一合法位置）。

格式模板（frontmatter 必填，正文风格参照 `languageLearning.agent.md`）：

```markdown
---
name: <AgentName>
description: <一句话：作用 + 何时使用>
---

# 角色定位

（一段话说明你是谁、擅长什么、服务什么场景）

# 核心规则

## <场景 A>
（该场景下如何行为的明确规则）

## <场景 B>
（同左）

# 输出格式要求

（输出语言、格式、语气、禁止事项；需要模板则给出模板）
```

规范：

- frontmatter 的 `name` 必须等于 agents.json 的 key，等于文件名主体
- `description` 用中文一句话，必须包含"何时使用"（供前端面板与 Agent 路由判断），与正文角色定位一致
- 正文从 `# 角色定位` 开始；规则要具体可执行，禁止空泛套话；输出格式给明确约束与示例模板

### 第 3 步 更新 `agents.json`

路径：`.deepagents/agents/agents.json`。在现有对象上**新增一个 key**（不删除、不改动其他 agent，尤其不动 `default`）。

写入结构：

```json
{
  "<name>": {
    "description": "<第 1 步确认的一句话描述>",
    "enabled": true,
    "subagents": [],
    "tools": {
      "inner_tools": [ "<用户选择的内置工具名，如 read_file、glob>" ],
      "mcp_tools": [
        {
          "server": "calc_server",
          "serverInfo": { "name": "Calculator Server", "title": null, "version": "3.1.1", "websiteUrl": null, "icons": null },
          "tools": [ /* manifest.json 中用户选中的工具定义，原样拷贝 */ ]
        }
      ]
    },
    "skills": [ "<用户选择的技能名，如 skill-creator>" ]
  }
}
```

写入细则：

- `inner_tools`：只写用户选中的内置工具名（字符串数组）；一个没选则为 `[]`
- `mcp_tools`：按服务聚合；每项从对应 `manifest.json` 原样拷贝 `serverInfo` 和 `tools`（**tools 只含用户选中的工具**，不要整包全塞）；知识库服务条目 `server` 固定为 `knowledge_server`
- `skills`：用户选了哪些技能就写哪些（数组）；一个不选则写 `[]`；**不要写 `null`**（`null` 语义是"全启用"，与用户选择不符）
- 用户选了任意知识库 → 必须在 `mcp_tools` 中加入 `knowledge_server` 条目（`serverInfo` 与 `tools` 从 `.deepagents/tools/mcp_servers/knowledge_server/manifest.json` 拷贝）
- JSON 缩进 2 空格、`ensure_ascii` 关闭（中文原文保存）；写完立即 `json.loads` 校验可解析

### 第 4 步 校验与汇报

- 检查 `.deepagents/agents/<name>.agent.md` 存在且 frontmatter 完整
- 检查 `agents.json` 可被 `json.loads` 解析，且新 key 存在
- 向用户汇报：Agent 名、文件路径、一句话功能说明、启用的技能/工具/知识库清单；说明新 agent 会在前端刷新或下次会话装配时生效（skills 由装配器按 agents.json 自动物化，无需手工维护 `.enabled` 目录）

## 注意事项

- 不要覆盖已存在的 agent（同名时先询问用户是否替换）
- 不要修改 `default` 或其他既有 agent 的配置
- 知识库服务离线时跳过第 5 项，不要伪造知识库数据
- `mcp_tools` 的工具定义必须来自真实 `manifest.json`，禁止手编参数结构
- 涉及中文描述一律中文，涉及文件名/JSON key 一律英文
