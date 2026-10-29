# 模型配置整合方案（mode_config.json）

日期：2026-10-29（v2 修订）
状态：已实施

## 背景

用户提供 4 个已验证文件（acp_agent.py / context_compactor.py / mcp_loader.py / model_util.py），要求分析差异做合并；模型/上下文配置统一收敛到根目录 **mode_config.json**（模型配置数组，格式以用户截图为准），并新增 `content_size` 字段作为前端上下文比例总数与压缩阈值的依据。

## mode_config.json（唯一配置文件）

格式：模型配置数组，每个条目含模型参数，**新增 `content_size`**（该模型的上下文窗口，tokens）与 `active`（当前生效标记）：

```json
[
  {
    "model_name": "gemini-2.5-flash",
    "model_provider": "google_genai",
    "endpoint": "https://api.ai-service.global.fujitsu.com/ai-【待补全】",
    "temperature": 0.1,
    "top_p": 0.1,
    "max_tokens": 65535,
    "content_size": 1048576,
    "active": false
  },
  {
    "model_name": "gemini-2.5-pro",
    "model_provider": "google_genai",
    "endpoint": "https://api.ai-service.global.fujitsu.com/ai-【待补全】",
    "temperature": 0.1,
    "top_p": 0.5,
    "max_tokens": 65535,
    "content_size": 1048576,
    "active": false
  },
  {
    "model_name": "qwen3.6-14b-a3a",
    "model_provider": "openai",
    "endpoint": "http://127.0.0.1:8088",
    "api_key": "-",
    "temperature": 0,
    "max_tokens": 20480,
    "top_p": 0.5,
    "content_size": 262144,
    "active": true
  }
]
```

说明：
- `content_size`：画面上上下文使用率的总数、context_compactor 的 CONTENT_SIZE 均以 **active 模型**的 content_size 为准（切换模型后自动变化）。
- `active`：当前生效模型（前端切换模型时由后端移动该标记）。
- Gemini 两个条目的 endpoint 来自用户截图，**完整 URL 被截图截断**，标注「待补全」；补全后即可切换使用。qwen3.6-14b-a3a 为当前本地可用模型（active）。

## 代码改动

### `utils/model_util.py`（采用附件版本逻辑）

- `create_model(model_name=None)` 工厂：每次调用重读 mode_config.json；model_name 缺省取 active 条目；timeout/max_retry 从 `.env` 读取（附件版行为）。
- 新增 `current_model_name()` / `current_content_size()` / `list_models()` 供 app.py 与 context_compactor 使用。
- 已删除旧的 env 全局单例 MODEL（其他模块不得再引用）。

### `utils/context_compactor.py`

- `CONTENT_SIZE = current_content_size() or 32768`（active 模型的 content_size）。
- 摘要模型用 `create_model()` 每次构建（附件版）。

### `acp_agent.py`

- `MODEL` → `create_model()`（task / Chat 分支），保留工作目录版 Chat 分支、ExecuteTimeoutGuard、完整 system_prompt。

### `api/timers_engine.py`

- `explain_cron_ai` 中 `MODEL` → `create_model()`。

### `tests/test_context_compactor.py`

- `cc.MODEL = fm` → `cc.create_model = lambda: fm`。

### `app.py`

- `GET /api/config` → `{ contentSize, model, models[] }`（contentSize = active 模型 content_size）。
- `POST /api/config {"model": "xxx"}` → 移动 active 标记并写回 mode_config.json。

### 前端 `web/src/pages/ChatPage.vue`

- `loadAppConfig` 读取 contentSize + 当前模型名 + 模型列表；`switchModel(name)` POST 切换。
- 输入框工具栏 content 使用率左侧显示当前模型名（HardwareChip 图标），hover 弹出模型列表点击切换；三语提示（modelSwitchSuccess / modelSwitchFailed）。

### `.env`

- 删除模型相关：MODEL_PROVIDER / API_KEY / ENDPOINT / MODEL_NAME / TEMPERATURE / TOP_P / MAX_TOKENS / STREAM / CONTENT_SIZE。
- **恢复 TIMEOUT=300 / MAX_RETRY=2**（附件版 model_util 从 env 读取）。
- 备份 `.env.model_backup`；.gitignore 追加 `**/.env.*`。

## 验证结果

- py_compile 全部通过；`tests/test_context_compactor.py` 17/17。
- `GET /api/config` → `{"contentSize":262144,"model":"qwen3.6-14b-a3a","models":["gemini-2.5-flash","gemini-2.5-pro","qwen3.6-14b-a3a"]}`。
- 切换 `gemini-2.5-flash` → `contentSize` 变 1048576、model 更新；切回 qwen 正常。
- 前端构建通过，浏览器实测显示当前模型名。
- 模型调用链路：`POST http://127.0.0.1:8088/chat/completions → HTTP 200`。

## 待办

- 用户提供 gemini-2.5-flash / gemini-2.5-pro 的完整 endpoint（截图截断处），补入 mode_config.json 后即可切换。
- 已知环境问题（与本次改造无关）：8088 端点当前加载 GLM-4.6V-Flash（推理模型，回复在 reasoning_content、content 为空），agent 流式对话可能无可见输出；建议端点换非推理模型或做 reasoning_content 适配。
