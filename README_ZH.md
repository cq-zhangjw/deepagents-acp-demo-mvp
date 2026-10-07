# DeepAgents + ACP + FastAPI Gateway

这是一个可直接运行的 ACP（Agent Client Protocol）Web 集成示例，核心思路是：

```
前端浏览器（ACP Client）
   ├─ POST /upload 上传图片/文件 → FastAPI 网关保存并返回 http 资源地址
   └─ WS /acp-ws → app.py 网关 → acp_agent.py 子进程（stdio ACP）
                                  └─ DeepAgents Agent → OpenAI 兼容模型服务
```

- **ACP 层**：使用 `deepagents-acp` + `agent-client-protocol`，自动处理
  `session/update`、`session/request_permission` 等协议消息，无需手写底层 JSON-RPC。
- **网关层**：`app.py` 负责 `WebSocket ⇄ stdio` 双向透传和文件上传，不解析业务协议。
- **前端**：`web/` 是基于 Vue 3 + TypeScript 的 SPA（Vite 构建、Naive UI），实现 ACP v2 客户端；构建产物 `web/dist` 由网关挂在根路径 `/` 上。`static/index.html` 仅作为遗留演示页保留。

## 目录结构

```text
./
├─ app.py                # FastAPI 网关（/upload + /acp-ws，根路径托管 web/dist）
├─ acp_agent.py          # DeepAgents ACP Agent（stdio 服务）
├─ utils/
│  └─ model_util.py     # 模型初始化配置（OpenAI 兼容接口）
├─ web/                  # Vue 3 + TypeScript ACP 客户端（Vite 构建 → web/dist）
│  ├─ src/              # 组件：ChatPage / ExecutionProcess / ToolCallCard / MarkdownMessage
│  └─ dist/             # 构建产物，由 app.py 挂在根路径
├─ static/
│  └─ index.html        # 遗留演示页（不再是活动前端）
├─ docs/                # 设计文档（DESIGN_ZH.md、UI_DESIGN.md）
├─ uploads/              # 上传文件存储目录（自动创建）
├─ db/                   # SQLite checkpoint 目录（自动创建）
├─ .env                  # 应用配置（HOST/PORT / 模型接口）
├─ requirements.txt
├─ LICENSE
├─ README.md
├─ README_ZH.md
├─ README_JA.md
└─ .venv/                # 可选：本地虚拟环境
```

## 当前实现说明

本项目的真实入口是 `app.py`，不是旧版 README 中的 `gateway.py`：

- `app.py` 启动 FastAPI 服务
- `@app.post("/upload")` 接收上传文件，保存到 `uploads/`，返回：
  - `uri`：可访问的 HTTP 地址
  - `name`：文件名
  - `mimeType`：MIME 类型
- `@app.websocket("/acp-ws")` 为每个 WebSocket 连接创建一个独立 `acp_agent.py` 子进程，并做双向转发
- `acp_agent.py` 中的 `build_agent()` 使用 `create_deep_agent(...)` 和 `interrupt_on` 来触发权限审批

## 运行方式

### 1）创建虚拟环境并安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### 2）配置环境变量

项目已提供 `.env`，示例内容如下：

```env
# [Common Settings]
PYTHONIOENCODING=utf-8
APP_ENV=pro

MODEL_PROVIDER=openai

TAVILY_API_KEY=-
TIMEOUT=300
MAX_RETRY=2

# [AI settings]
API_KEY=-
ENDPOINT=http://192.168.3.28:8088
MODEL_NAME=qwen3.8-9b
TEMPERATURE=0.8
TOP_P=0.5
MAX_TOKENS=10240

# [App settings]
APP_HOST=0.0.0.0
APP_PORT=8000
CONTENT_SIZE=32768
UPLOAD_ROOT=./uploads
```

其中：
- `ENDPOINT` 是 OpenAI 兼容接口地址
- `API_KEY` 是对应模型服务的访问 key
- `APP_HOST` / `APP_PORT` 控制 FastAPI 监听地址
- `CONTENT_SIZE` 是上下文窗口大小（tokens），用于前端上下文用量百分比显示（经 `/api/config` 下发）
- `UPLOAD_ROOT` 是文件上传存储根目录（相对项目根或绝对路径，默认 `uploads/`），该目录已在 `.gitignore` 中忽略

如果你正在直接对接 OpenAI 官方接口，也可以按自己的环境变量方式配置；当前仓库默认是接到自建兼容服务。

### 3）启动网关

```powershell
.\.venv\Scripts\python app.py
```

默认监听：
- `http://127.0.0.1:8000`

### 4）打开前端页面

```text
http://127.0.0.1:8000/
```

根路径托管构建好的 Vue SPA（`web/dist`）。

## 前端交互流程

### 文件上传

前端选择图片/文件后，会先调用：

```http
POST /upload
Content-Type: multipart/form-data
```

返回结构类似：

```json
{
  "uri": "http://127.0.0.1:8000/uploads/xxx.png",
  "name": "xxx.png",
  "mimeType": "image/png"
}
```

随后前端把该资源加入 `session/prompt` 内容列表：普通文件作为虚拟路径文本上下文（如 `/uploads/xxx.png`），图片作为 ACP `image` 内容块。

### ACP 会话链路

前端会按如下顺序执行：

1. `initialize`：建立 ACP 连接并协商协议版本
2. `session/new`：首次创建会话
3. `session/prompt`：发送用户任务和可选资源
4. `session/update`：持续接收Agent状态更新
5. `session/request_permission`：当 agent 需要用户确认时弹出审批框

## 会话复用逻辑

前端已实现多轮对话复用与历史恢复：

- 首次提交：自动创建新会话
- 后续提交：复用当前 `sessionId`，Agent 会保留会话记忆
- 若用户点击「新建会话」：重置会话并丢弃旧记忆
- 刷新页面或 WebSocket 断开：界面从 `localStorage` 恢复会话，并通过 `session/load` 从 SQLite checkpoint 重放 Agent 历史

核心原理：
- 每个 WebSocket 连接都会 spawn 一个 `acp_agent.py` 子进程
- `AgentServerACP` 开启 `load_sessions=True`，按 `sessionId` 将 LangGraph checkpoint 持久化到 `db/agent_state.sqlite`
- 同一连接下多次 `session/prompt` 即可形成多轮交互；重连后通过 `session/load` 恢复历史

## 权限审批机制

`acp_agent.py` 中已经启用了 `interrupt_on`，当前配置会在高风险操作前触发 HITL 中断，并转成 ACP 的 `session/request_permission`：

```python
interrupt_on={
    "execute": False,     # shell 命令默认放行（按命令类型始终允许）
    "delete": True,       # 删除操作需要用户审批
}
```

前端会弹出权限确认框，用户可选择：
- 允许
- 拒绝
- 始终允许

对应返回：

```json
{ "outcome": { "outcome": "selected", "optionId": "approve" } }
```

> 前端通过相同的消息 id 回复权限结果，确保 ACP 请求-响应匹配。

## 模型与工具配置

模型初始化位于 [utils/model_util.py](utils/model_util.py)：

```python
MODEL = init_chat_model(
    MODEL_NAME,
    model_provider=MODEL_PROVIDER,
    base_url=ENDPOINT,
    api_key=API_KEY,
    timeout=TIMEOUT,
    max_retries=MAX_RETRY,
    temperature=TEMPERATURE,
    top_p=TOP_P,
    max_tokens=MAX_TOKENS,
)
```

前项目默认使用 OpenAI 兼容服务，且模型名和地址可以直接在 `.env` 中调整。

## 典型使用例子

### 任务：分析图片并生成配置

1. 在前端输入任务：
   - 「帮我分析上传的图片，并生成一份项目配置」
2. 选择图片文件
3. 点击「提交任务」
4. Agent 会输出：
   - 执行计划
   - 流式文本输出
   - 工具调用卡片
   - 最终总结

### 任务：要求执行命令

如果 Agent 需要执行 shell 命令，系统会弹出权限审批，用户可决定放行或拒绝。

## 常见问题排查

### 1. 页面点击“提交任务”没有反应

检查项：
- 是否已启动后端：`http://127.0.0.1:8000/docs` 是否可访问
- 是否已刷新页面（Ctrl + F5）
- 前端底部 JS 日志是否有报错

### 2. 模型返回 “Missing credentials” / “Connection error”

通常表示：
- `.env` 中 `API_KEY`/`ENDPOINT` 配置不正确
- 模型服务未启动或地址不通
- OpenAI 兼容服务拒绝了请求

### 3. 端口被占用

可修改 `.env` 中的：

```env
APP_PORT=8000
```

或者在启动前关闭占用端口的进程。

## 已知限制

- 每个 WebSocket 连接启动一个独立 Agent 子进程，适合 demo/单用户场景
- 上传文件没有配额与清理策略
- 没有鉴权机制，生产环境建议增加 token 校验
- 当前示例偏演示型，适合继续扩展成多会话/多租户架构

## 结论

这个项目的当前实现重点是：

- `app.py` 提供 FastAPI 网关
- `acp_agent.py` 提供 ACP 运行时
- `web/` 提供 Vue 3 SPA 交互界面（构建产物 `web/dist` 由网关托管）
- `.env` / `utils/model_util.py` 控制模型接入配置

对于开发者而言，它既可以作为 ACP 接入样例，也可以作为前后端桥接的基础模板继续扩展。
