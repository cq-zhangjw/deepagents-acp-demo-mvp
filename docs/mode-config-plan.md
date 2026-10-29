# 模型与上下文配置整合方案（mode_config.json）

日期：2026-10-29
状态：已实施

## 背景

后端模型/上下文相关配置此前散落在 `.env`（MODEL_NAME / ENDPOINT / TEMPERATURE / MAX_TOKENS / CONTENT_SIZE / TIMEOUT / MAX_RETRY 等）。本次将配置统一收敛到 JSON 文件，并支持前端展示/切换当前模型。

## 文件结构

### 1. `mode_config.json`（运行模式配置，根目录）

```json
{
  "content_size": 262144,
  "model": "qwen3.6-14b-a3a",
  "timeout": 300,
  "max_retry": 2
}
```

- `content_size`：上下文窗口大小（tokens），**前端上下文使用率的总数以它为准**，同时是 context_compactor 的 CONTENT_SIZE 来源（原 `.env` 的 CONTENT_SIZE）。
- `model`：当前生效模型名，前端可点击切换（POST /api/config 写入）。
- `timeout` / `max_retry`：模型请求超时（秒）与重试次数（原 `.env` TIMEOUT / MAX_RETRY）。

### 2. `model_config.json`（模型配置列表，根目录）

```json
[
  {
    "model_name": "qwen3.6-14b-a3a",
    "model_provider": "openai",
    "endpoint": "http://127.0.0.1:8088",
    "api_key": "-",
    "temperature": 0,
    "max_tokens": 20480,
    "top_p": 0.5
  }
]
```

- 每项一条模型配置，供 `utils/model_util.create_model(model_name)` 按名构建模型。
- 初始列表来自：`.env` 当前配置（qwen3.6-14b-a3a）、`.env.bak` 历史配置（Ornith-1.5-9B）、端点探测（GLM-4.6V-Flash-UD-Q6_K_XL.gguf，llama.cpp 实际加载）。
- 新增/调整模型：直接编辑此文件。

## 代码改动

### `utils/model_util.py`（重写，采用上传附件版本为基础）

- `create_model(model_name=None)`：每次调用重新加载 mode_config.json / model_config.json；`model_name` 缺省取 `mode_config.json["model"]`。
- 新增 `load_mode_config()` / `current_model_name()` / `list_models()` 供 app.py 与 context_compactor 使用。
- 移除了旧的 env 读取与全局单例 MODEL（注意：**其他模块不得再引用 `MODEL`**）。

### `utils/context_compactor.py`

- `CONTENT_SIZE = int(load_mode_config().get("content_size") or 32768)`（不再读 env）。
- 摘要模型改用 `create_model()` 每次构建（对齐附件版本）。

### `acp_agent.py`

- `from utils.model_util import MODEL` → `create_model`；task 分支与 Chat 分支均 `model=create_model()`。
- 保留工作目录版的 ExecuteTimeoutGuard、完整 system_prompt、Chat 分支、todo 工具描述。

### `api/timers_engine.py`

- `explain_cron_ai` 中 `MODEL` → `create_model()`（原引用已删除的全局单例，必须同步）。

### `tests/test_context_compactor.py`

- `cc.MODEL = fm` → `cc.create_model = lambda: fm`。

### `app.py`

- `GET /api/config`：返回 `{ contentSize, model, models[] }`（contentSize 来自 mode_config.json）。
- `POST /api/config`：`{ "model": "xxx" }` 写入 mode_config.json（切换当前模型）。

### 前端 `web/src/pages/ChatPage.vue`

- `loadContextSize` → `loadAppConfig`：读取 contentSize + 当前模型名 + 模型列表。
- 新增 `currentModel` / `modelList` ref 与 `switchModel(name)`（POST /api/config）。
- 输入框工具栏右侧、content 使用率左侧新增模型名 pill（HardwareChip 图标），hover 弹出模型列表点击切换，当前模型 ✓ 高亮。
- locale（zh/ja/en）新增 `modelSwitchSuccess` / `modelSwitchFailed`。

### `.env` 清理

删除：`MODEL_PROVIDER` / `API_KEY` / `ENDPOINT` / `MODEL_NAME` / `TEMPERATURE` / `TOP_P` / `MAX_TOKENS` / `STREAM` / `CONTENT_SIZE` / `TIMEOUT` / `MAX_RETRY`。
保留：TTS、MOOFILE、CONTEXT_COMPACT_*（压缩阈值等仍走 env）、TIMER、TAVILY_API_KEY 等。
改动前已备份为 `.env.model_backup`。

## 验证结果

- `py_compile` 全部通过；`tests/test_context_compactor.py` 17/17 断言通过。
- `GET /api/config` → `{"contentSize":262144,"model":"qwen3.6-14b-a3a","models":[...]}`。
- `POST /api/config {"model":"Ornith-1.5-9B"}` → mode_config.json 更新、GET 返回新值；验证后已恢复默认模型。
- 前端构建通过，浏览器实测右下角显示模型名 `qwen3.6-14b-a3a`（发送按钮左侧）。
- 模型调用链路正常：`POST http://127.0.0.1:8088/chat/completions → HTTP 200`。

## 已知问题（与本次改造无关，供参考）

- 模型端点 8088（llama.cpp）当前实际加载 **GLM-4.6V-Flash-UD-Q6_K_XL.gguf**（推理模型，9.4B）。其回复放在 `reasoning_content` 字段、`content` 为空，deepagents 流式对话可能因此无可见输出（agent 收到空 content 后停滞）。此为模型服务行为，非本次配置整合引入；如需 Agent 正常对话，建议在 8088 端点加载非推理模型（如 qwen 系 / Ornith），或对 reasoning_content 做适配。
- `chat.agent.md` 文件名小写问题（Linux 部署需统一大小写）见 docs/chat-task-mode-plan.md。
