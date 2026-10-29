# 模型配置整合方案（model_config.json）

日期：2026-10-29（v3 修订）
状态：已实施

## 背景

用户提供 4 个已验证文件（acp_agent.py / context_compactor.py / mcp_loader.py / model_util.py），要求分析差异做合并；模型配置统一收敛到根目录 **model_config.json**（模型配置数组，格式以用户截图为准）。v3 按用户澄清重构：

1. 文件名统一为 `model_config.json`（非 mode_config.json）。
2. **不使用 active 字段**：用哪个模型以前端选择为准，前端把模型名传给后台（参考 agent_name 的传递——WS 查询参数 → 环境变量），读取配置时按该模型名获取对应配置。
3. 模型配置只保留本地 `qwen3.6-14b-a3a`，移除其它条目。

## model_config.json（唯一配置文件）

```json
[
  {
    "model_name": "qwen3.6-14b-a3a",
    "model_provider": "openai",
    "endpoint": "http://127.0.0.1:8088",
    "api_key": "-",
    "temperature": 0,
    "max_tokens": 20480,
    "top_p": 0.5,
    "content_size": 262144
  }
]
```

说明：
- `content_size`：该模型的上下文窗口（tokens），前端上下文使用率的总数、context_compactor 的 CONTENT_SIZE 均以**当前选择的模型**为准。
- 无 active / 无全局 model 字段——模型名由前端经 WS 查询参数注入。

## 模型名传递链路（对齐 agent_name）

```
前端选择模型（模型 pill 点击 / 会话恢复）
  → conversation.model 字段持久化（/api/history，同 mode）
  → WS 连接 /acp-ws?agent=xxx&model=yyy
  → app.py 注入环境变量 MODEL_NAME=yyy
  → acp_agent 子进程 create_model() 缺省取 MODEL_NAME
  → model_util.get_model_config(model_name) 从 model_config.json 取配置
```

## 代码改动

### `utils/model_util.py`

- `MODEL_CONFIG_PATH = BASE_DIR / "model_config.json"`。
- `current_model_name()`：优先读环境变量 `MODEL_NAME`（WS 注入，参考 AGENT_NAME）；未注入时回退 model_config.json 第一个模型。
- `current_content_size()`：当前模型的 content_size；缺省 0（调用方兜底 32768）。
- `get_model_config(model_name)`：按名取配置，找不到抛 ValueError（附可用列表）。
- `list_models()`：返回完整配置数组。
- `create_model(model_name=None)`：缺省 current_model_name()；timeout/max_retry 从 `.env` 读取（附件版行为）。

### `app.py`

- WS `/acp-ws`：读取 `?model=` 查询参数，注入 `agent_env["MODEL_NAME"]`。
- `GET /api/config`：返回 `{ models: [{ model_name, content_size }] }`（来源 model_config.json，供前端展示与切换）。
- **删除 POST /api/config**（不再需要写回 active）。

### `utils/context_compactor.py`

- `CONTENT_SIZE = current_content_size() or 32768`（当前选择模型的 content_size）。
- 摘要模型用 `create_model()`（每次构建，附件版）。

### `acp_agent.py` / `api/timers_engine.py`

- `MODEL` → `create_model()`（task / Chat 分支 / 定时任务 explain_cron_ai），无参调用自动跟随 WS 注入的 MODEL_NAME。

### `tests/test_context_compactor.py`

- `cc.MODEL = fm` → `cc.create_model = lambda: fm`。

### 前端 `web/src/pages/ChatPage.vue`

- `Conversation` 接口新增 `model?: string`（会话级模型，随 /api/history 持久化，同 mode）。
- `loadAppConfig`：读取 models 列表（model_name + content_size）；currentModel 恢复顺序 = 当前会话记录 > localStorage（`chat_model`）> 第一个模型。
- `contextSize` 改为 computed：随当前模型 content_size 变化，驱动上下文使用率百分比。
- `switchModel(name)`：本地更新（不再 POST）→ 写 localStorage → 写入会话并保存 → 重置 WS 连接（新模型经 WS 参数传递）。
- `websocketUrl()`：`/acp-ws?agent=...&model=...`。
- `createConversation`：新会话带当前模型；`selectConversation`：进入会话时恢复其 model（无记录保持当前选择）。
- 模板模型 pill / picker 使用 `model_name` 字段。

## 验证结果

- py_compile 全部通过；`tests/test_context_compactor.py` 17/17。
- `GET /api/config` → `{"models":[{"model_name":"qwen3.6-14b-a3a","content_size":262144}]}`；POST /api/config 已不存在（405）。
- env 注入测试：`MODEL_NAME=qwen3.6-14b-a3a` → current_content_size 262144。
- 前端构建通过，浏览器实测右下角显示 `qwen3.6-14b-a3a`，无报错。

## 待办

- 已知环境问题（与本次改造无关）：8088 端点当前加载 GLM-4.6V-Flash（推理模型，回复在 reasoning_content、content 为空），agent 流式对话可能无可见输出；建议端点换非推理模型或做 reasoning_content 适配。
