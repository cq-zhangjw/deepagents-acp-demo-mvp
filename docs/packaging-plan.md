# 打包发布方案（Packaging Plan）

> 状态：**方案待定稿**（已评估，未实施）
> 日期：2026-10-15
> 目标：把 DeepAgents 工作台打包给其他人在 Windows 上**免安装环境**使用（双击即用）。

---

## 1. 可行性评估结论

**结论：可行（PyInstaller onedir 便携版），但有 3 个前提。**

技术栈核查结果：

- 后端 FastAPI + uvicorn + langchain/langgraph + deepagents + moofile + fastmcp，前端 Vue 已 build 成静态 `web/dist` 由后端托管，结构适合打包。
- 子进程：`AGENT_CMD = [sys.executable, "acp_agent.py"]`，MCP 服务器走 fastmcp stdio 子进程 —— **打包后必须改造进程启动方式**（见 §4.1/§4.2）。
- 数据与配置：`db/`、`uploads/` 运行时自建；`models/`（TTS onnx）、`.deepagents/`（agents/skills/tools）、`web/dist` 需随包带上。
- 外部依赖：`knowledge_server`（MCP 外部服务）离线时可跳过，不阻塞；**但模型端点 `http://192.168.3.28:8088` 是内网地址，他人电脑访问不到** —— 这是打包能否「开箱即用」的最大变量。

### 1.1 三个前提

1. **模型端点必须用户自配**：包内不含模型服务；用户需填写自己的 OpenAI 兼容端点（`ENDPOINT` / `API_KEY` / `MODEL_NAME`）。
2. **子进程改造**：`acp_agent.py` 打成第二个 exe（onedir 多入口），打包后 `AGENT_CMD` 指向同目录 `acp_agent.exe`；fastmcp MCP 服务器子进程同理。
3. **动态导入依赖收集**：langchain / langgraph / deepagents / fastmcp / moofile 等需 `--collect-all` 收集并打包后试跑验证。

---

## 2. 方案对比

| 方案 | 是否装环境 | 体积 / 启动 | 风险 | 结论 |
| --- | --- | --- | --- | --- |
| **PyInstaller onedir 便携版** | 不需要 | ~1–2GB，启动 3–8s | langchain/fastmcp 动态导入需收集调优 | **推荐** |
| Docker 镜像 | 需装 Docker | 大 | 用户要装 Docker，违背诉求 | 排除 |
| 嵌入式 Python 绿色版 | 不需要 | 同 PyInstaller | 维护繁琐 | 不推荐 |
| PyInstaller onefile | 不需要 | 单文件但启动慢、子进程与可写数据难处理 | 不适合多进程 + 写 db | 排除 |

---

## 3. 推荐方案：PyInstaller onedir 绿色便携包

### 3.1 交付形态

```
DeepAgents-v3.1-win64/
├─ DeepAgents.exe      # 双击启动：拉起后端 + 自动打开浏览器
├─ acp_agent.exe       # Agent 子进程（第二个打包入口）
├─ _internal/          # 全部依赖库（打包自动生成）
├─ web/dist/           # 前端静态资源（保持不变）
├─ models/             # TTS onnx 模型（保持不变）
├─ .deepagents/        # agents/skills/tools（保持不变）
├─ .env                # 首次运行需配置模型端点
└─ db/  uploads/       # 运行时自动创建
```

### 3.2 使用流程

1. 解压 zip。
2. 编辑 `.env`，填写自己的 `ENDPOINT` / `API_KEY` / `MODEL_NAME`。
3. 双击 `DeepAgents.exe`。
4. 浏览器自动打开 `http://127.0.0.1:8000`。

### 3.3 目录结构对照（数据层不变）

| 开发环境 | 打包后 | 说明 |
| --- | --- | --- |
| `app.py` / `acp_agent.py` | `DeepAgents.exe` / `acp_agent.exe` | 编译后的程序 |
| `web/dist/` | `web/dist/` | 不变（前端静态资源） |
| `models/` | `models/` | 不变（TTS 模型） |
| `.deepagents/` | `.deepagents/` | 不变（agents/skills/tools） |
| `.env` | `.env` | 不变（用户改模型端点） |
| `db/` `uploads/` | `db/` `uploads/` | 不变（运行时自动创建） |
| `.venv/Lib/site-packages` | `_internal/` | 唯一变化：依赖库集中在此 |

> 数据 / 配置 / 资源目录全部在 exe 同目录，路径引用与现在完全一致。

---

## 4. 具体实施步骤

### 4.1 路径基座改造（必做）

当前 `app.py`：`BASE_DIR = Path(__file__).resolve().parent` —— PyInstaller 打包后 `__file__` 指向 `_internal` 内部，路径会错乱。

需改为（frozen 适配）：

```python
import sys

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
```

`.env` 加载显式指向：

```python
load_dotenv(BASE_DIR / ".env")
```

`WEB_DIST_DIR`、`UPLOAD_DIR`、`AGENT_CMD` 均基于 `BASE_DIR`，随之修正。

### 4.2 子进程启动改造（必做）

`AGENT_CMD = [sys.executable, str(BASE_DIR / "acp_agent.py")]` —— 打包后 `sys.executable` 是 `DeepAgents.exe`，不接受 `.py` 参数。

改造为 frozen 检测，指向同目录 `acp_agent.exe`（acp_agent.py 单独作为第二个 PyInstaller 入口）：

```python
if getattr(sys, "frozen", False):
    AGENT_CMD = [str(BASE_DIR / "acp_agent.exe")]
else:
    AGENT_CMD = [sys.executable, str(BASE_DIR / "acp_agent.py")]
```

fastmcp MCP 服务器子进程（`.deepagents/tools/mcp_servers/*/manifest.json` 对应入口）同理：打包后统一改为调用同目录的对应 exe（或集中用一个 `mcp_runner.exe`）。

### 4.3 依赖收集（PyInstaller spec 要点）

- 需要 `--collect-all`（或 spec 中 `collect_all`）的包：
  - `langchain`、`langchain_core`、`langchain_openai`、`langgraph`、`langgraph_checkpoint_sqlite`
  - `deepagents`、`deepagents_acp`
  - `fastmcp`、`moofile`
- 本地 wheel：`./whls/jieba_fast-0.53-cp312-cp312-win_amd64.whl` 需先装入打包机 venv（PyInstaller 才能收集到）。
- `onnxruntime` + `genie_tts` + `models/`：模型目录作为 datas 打入。
- `playwright`：代码使用系统 MS Edge（`channel="msedge"`），无需内置浏览器；若打包遇到驱动路径问题，用 `--collect-data playwright` 收集。
- 多入口：`app.py` → `DeepAgents.exe`，`acp_agent.py` → `acp_agent.exe`（PyInstaller spec 支持多 Analysis/PYZ/EXE）。

### 4.4 数据文件（datas）

| 源 | 目标（exe 同目录） |
| --- | --- |
| `web/dist/` | `web/dist/` |
| `models/` | `models/` |
| `.deepagents/` | `.deepagents/` |
| `.env.example`（或默认 `.env` 模板） | `.env` |

### 4.5 首启引导

- 启动时检测 `.env` 是否缺少有效 `ENDPOINT` / `API_KEY` → 打开配置说明页（或控制台提示 + 前端明确报错）。
- 模型不可达时，现有前端错误处理已能展示明确错误，无需额外开发。

### 4.6 验证（干净环境试跑）

打包后在**无 Python 的机器 / 虚拟机**验证全链路：

- [ ] 双击 exe 启动、浏览器自动打开
- [ ] 普通聊天：发送 → 流式回复
- [ ] 工具调用（内置工具 + 本地 MCP）
- [ ] 定时任务：创建 / 手动运行 / 执行履历
- [ ] 附件上传 / 图片预览
- [ ] TTS 语音（models/ 随包生效）
- [ ] 会话持久化（重启后履历恢复）

### 4.7 交付

- 压缩为 `DeepAgents-v3.1-win64.zip`。
- 附中文使用说明（配置模型端点 + 常见问题：端口占用、模型不可达、杀毒软件误报）。

---

## 5. 待确认事项（实施前需用户拍板）

1. **目标平台**：仅 Windows x64？（依赖含 `jieba_fast` win_amd64 wheel；跨平台需另行处理）
2. **模型端点策略（关键）**：
   - A) 用户自配自己的 OpenAI 兼容端点（推荐，包内不带模型服务）
   - B) 内网部署且端点常开，包内默认指向内网固定端点
   - C) 两者结合：默认值 + 允许覆盖
3. **MCP 外部服务**：`knowledge_server`、SharePoint 等依赖外部登录/服务的工具是否保留？（建议默认只带本地 skills/tools，减小体积与配置复杂度）

---

## 6. 风险与注意事项

- **体积**：langchain/deepagents/onnxruntime 生态较大，onedir 预计 1–2GB，zip 后可能 500MB–1GB。
- **启动速度**：onedir 首次启动 3–8s 属正常（onefile 更慢，已排除）。
- **杀毒软件误报**：PyInstaller 打包的 exe 可能被 Windows Defender 误报，需提示用户添加信任。
- **数据可写性**：exe 所在目录需可写（`db/`、`uploads/` 运行时创建）；不要放在 `Program Files` 等受保护目录。
- **版本联动**：每次功能迭代后需重新打包；tag（如 v3.1）与打包版本保持一致。
- **子进程路径**：任何新增的 python 子进程（MCP server、agent）都必须走 frozen 分支，否则打包后运行报错。
