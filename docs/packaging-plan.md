# 打包详细实施方案（Packaging Implementation Plan）

> 状态：**方案定稿，待实施**
> 日期：2026-10-17
> 范围：Windows 免安装便携分发（PyInstaller onedir 双包）；**本期不含认证登录**（认证另行评估，不阻塞打包）
> 目标读者：实施者（可照步骤执行）

---

## 1. 交付形态

**基线：PyInstaller onedir 便携 zip。** 双包策略：

| 包 | 内容 | 体积（zip 后估算） | 说明 |
| --- | --- | --- | --- |
| `DeepAgents-vX.Y-win64-base.zip` | 程序 + web/dist + .deepagents + 依赖 | ~700MB–1GB | 默认分发（语音引擎在线 edge_tts / 系统 sapi，无需本地模型） |
| `DeepAgents-vX.Y-win64-full.zip` | 上述 + 可选本地 TTS 模型（`models/`，预留） | ~1.5–2GB | 需要开箱本地语音（当前引擎为在线 edge_tts / 系统 sapi，模型非必需） |

- 不追 onefile（多进程 + 可写数据不适合）；不追 Docker（违背双击即用）。
- 本期不实施安装器/签名/自动更新（列在"后续阶段"），但路径架构为其预留。

---

## 2. 包目录结构（目标）

```
DeepAgents-vX.Y-win64/
├─ DeepAgents.exe          # 主入口：拉起 uvicorn + 自动打开浏览器
├─ acp_agent.exe           # Agent 子进程（第二入口）
├─ mcp_runner.exe          # 统一 MCP stdio 服务器入口
├─ _internal/              # 全部 python 依赖（PyInstaller 生成，勿动）
├─ web/dist/               # 前端静态资源（随包）
├─ .deepagents/            # agents / skills / tools（随包）
├─ models/                 # 语音模型（full 包有内容；base 包为空 + 引导）
├─ .env                    # 模型端点配置（首启向导写入）
├─ db/  uploads/           # 运行时自动创建（会话/分组/附件）
└─ README.txt              # 使用说明（解压即用、端点配置、数据备份）
```

---

## 3. 代码改造步骤（实施清单）

### 3.1 路径基座改造

**文件：`app.py`（含 acp_agent.py 同样处理）**

```python
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

# 程序/资源根目录：打包后 = exe 所在目录；开发环境 = 项目根
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# 数据目录：便携版与 BASE_DIR 同；安装版阶段切 %APPDATA%\DeepAgents（本期不启用，抽象预留）
DATA_DIR = BASE_DIR
```

改动点（逐一核对）：
1. `load_dotenv(...)` → `load_dotenv(BASE_DIR / ".env")`（显式路径）。
2. `WEB_DIST_DIR` → `BASE_DIR / "web" / "dist"`。
3. `UPLOAD_DIR`、`models/`、`.deepagents/`、`db/` 相关常量 → 全部基于 `BASE_DIR` / `DATA_DIR`。
4. `_PROJECT_ROOT` 类常量（api 各模块若引用 `__file__.parent.parent`）→ 统一改为从 `app` 导入的 `BASE_DIR`（或提供 `utils.paths` 单点）。**原则：打包后所有路径不能依赖 `__file__` 相对项目根。**

### 3.2 子进程启动改造

**文件：`app.py`（AGENT_CMD 定义处）**

```python
if FROZEN:
    AGENT_CMD = [str(BASE_DIR / "acp_agent.exe")]
else:
    AGENT_CMD = [sys.executable, str(BASE_DIR / "acp_agent.py")]
```

**MCP 服务器统一入口（新增 `mcp_runner.py`，瘦入口）**

```python
"""统一 MCP stdio 服务器启动器。
用法：mcp_runner(.exe) --server <服务目录名>
按 manifest.json 找到入口脚本，import 后运行（stdio）。
"""
import argparse, importlib, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    args = ap.parse_args()
    # 定位 .deepagents/tools/mcp_servers/<server>/manifest.json -> 入口模块
    # import 该模块（模块级 FastMCP 实例），调用其 run(transport="stdio")
    ...

if __name__ == "__main__":
    main()
```

- 打包后：`mcp_runner.exe` 是第三个 PyInstaller 入口；
- 工具加载侧：解析 manifest 时按 `FROZEN` 把 `command` 改写为 `[mcp_runner.exe, "--server", <name>]`（**不修改 manifest 源文件**，运行时改写）；
- 开发环境：仍走原 python 脚本路径。

### 3.3 端口与单实例（新增工具函数）

**文件：新增 `utils/runtime.py`**

```python
def pick_free_port(preferred: int = 8000) -> int:
    # 依次尝试 preferred..preferred+50，绑定检测；返回可用端口

def ensure_single_instance(port: int) -> bool:
    # 若端口已被本程序实例占用：返回 False（调用方提示"已在运行"并退出）
    # 用 socket connect 127.0.0.1:port 探测 + 本地锁文件（锁内写 pid）
```

app.py 启动逻辑：
- 端口被占 → `pick_free_port` 换端口，并把实际端口写入 `BASE_DIR/.runtime`（前端静态由后端同进程托管，无跨进程问题）；
- 重复双击 → 检测到已有实例 → 输出提示并退出（不破坏 db）。

### 3.4 首启配置向导（不含认证）

**后端新增 `api/setup.py`**

| 接口 | 说明 |
| --- | --- |
| `GET /api/setup/status` | `{ configured: bool }`——`.env` 存在且有 `ENDPOINT` 时为 true |
| `POST /api/setup/config` | body `{ endpoint, apiKey, modelName, language }` → 写入 `.env`（覆盖/新建），返回 ok |
| `POST /api/setup/test` | body `{ endpoint, apiKey, modelName }` → 请求 `${endpoint}/v1/models`（或最小空对话）测连通，返回 `{ ok, latencyMs, error? }` |

**前端新增 `SetupPage.vue`（路由 `/setup`）**
- 字段：端点、API Key、模型名、语言（三语）；
- 步骤：填表 → 连通性测试（按钮 + 结果展示）→ 保存；
- 语音模型缺失提示（base 包）：给出下载地址说明/导入路径指引；
- 保存成功 → `router.replace('/')`。

**路由守卫联动**（本期仅判断 setup，不涉及认证）：
- `main.ts` 启动时 `GET /api/setup/status`；未配置 → `location.hash = '#/setup'`；
- `router.beforeEach`：目标非 `/setup` 且未配置 → 跳 `/setup`。

### 3.5 前端其他适配

- 无改动：现有 UI 全部走相对路径 `/api/*`，同进程托管，打包不变。

---

## 4. PyInstaller 配置

### 4.1 打包机准备（Windows 10/11 x64，python 3.12）

```powershell
# 1) 创建独立打包 venv（与开发 .venv 分离，避免污染）
python -m venv .packvenv
.packvenv\Scripts\pip install -r requirements.txt
.packvenv\Scripts\pip install pyinstaller==6.*
```

### 4.2 spec 要点（`packaging/DeepAgents.spec`）

- **多入口**：三个 Analysis/PYZ/EXE 块：
  1. `app.py` → `DeepAgents.exe`（console=False）
  2. `acp_agent.py` → `acp_agent.exe`（console=True，子进程需 stdout 管道）
  3. `mcp_runner.py` → `mcp_runner.exe`（console=True）
- **collect_all**（动态导入包）：
  `langchain, langchain_core, langchain_openai, langgraph, langgraph_checkpoint_sqlite, deepagents, deepagents_acp, fastmcp, moofile`
- **binaries**：`playwright`（必要时 collect_data，浏览器走系统 MS Edge）
- **datas**：
  - `web/dist` → `web/dist`
  - `.deepagents` → `.deepagents`
  - `models` → `models`（**仅 full 包构建时启用**；base 包用空 models 占位）
  - `db/.gitkeep` 不需要（运行时自建）
- **excludes**：`tests`、`docs`、`whls`、`*.md`、`_*.png`、`.venv`、`.packvenv`
- 每个 EXE 的 `name` 固定，避免 PyInstaller 随机目录名。

### 4.3 构建脚本（`packaging/build.ps1`）

```powershell
# 参数：-Version 3.4 -Full（含模型）| -Base（默认）
param([string]$Version = "3.4", [switch]$Full)
$root = (Resolve-Path "..").Path
# 1) 前端构建
Push-Location "$root\web"; npm run build; Pop-Location
# 2) 生成 spec（可选按 -Full 调整 models datas）
# 3) 三入口打包
pyinstaller --clean --noconfirm packaging\DeepAgents.spec
# 4) 组装目录 + zip
$dist = "dist\DeepAgents-v$Version-win64"
Copy web\dist, .deepagents, models(按 Full), README.txt 到 $dist
Compress-Archive -Path $dist -DestinationPath "DeepAgents-v$Version-win64-$(if($Full){'full'}else{'base'}).zip"
```

---

## 5. 验证清单（干净机器上执行）

准备一台**无 python / 无 venv / 未装本项目依赖**的 Windows 机器（或临时 VM）：

| # | 场景 | 期望 |
| --- | --- | --- |
| 1 | 解压 base 包，双击 DeepAgents.exe | 服务启动、浏览器自动打开向导页 |
| 2 | 向导配置内网/可用 OpenAI 兼容端点 + 连通性测试 | 测试通过、保存后进入主界面 |
| 3 | 新建任务会话并发送任务 | agent 子进程（acp_agent.exe）执行、结果回传 |
| 4 | 切换/新建自定义 agent | 正常（agent 配置读 .deepagents/agents） |
| 5 | 启用至少一个本地 MCP 工具并调用 | mcp_runner.exe 路径可用 |
| 6 | 定时任务创建与触发 | 正常调度 |
| 7 | 附件上传 + 预览 + 会话持久化（重启后数据在） | 正常 |
| 8 | 语音（full 包）：中/日/英 TTS 各一次 | 正常出声 |
| 9 | 语音（base 包）：向导提示模型缺失 | 引导正确 |
| 10 | 端口被占（手动占用 8000 再启动） | 自动换端口并可用 |
| 11 | 重复双击 | 提示已在运行，不破坏数据 |
| 12 | db/ uploads/ 目录自动创建在 exe 旁 | 符合预期 |
| 13 | 杀软/SmartScreen 表现记录 | 写进 Release 说明 |

打包机本机先跑一遍，再换干净机器跑一遍。

---

## 6. 发布（GitHub Release）

- 命名：`DeepAgents-v{Version}-win64-base.zip` / `-full.zip`；
- Release 说明模板（`packaging/RELEASE_TEMPLATE.md`）：
  - 解压即用步骤（含"更多信息→仍要运行"说明，未签名）；
  - 端点配置说明（任意 OpenAI 兼容端点）；
  - 语音模型：full 包开箱；base 包下载/导入指引；
  - **数据备份提示**：升级只替换程序文件，勿删 `db/ uploads/ .env / models/`；
  - 已知限制（SmartScreen 误报、未签名）。

---

## 7. 实施工作量

| 项 | 估计 |
| --- | --- |
| 路径基座 + 子进程改造 + mcp_runner | 0.5–1 天 |
| 端口/单实例 + 首启向导（后端 + SetupPage） | 1–1.5 天 |
| PyInstaller spec + build.ps1 + 打包机验证 | 1–2 天 |
| 干净机器验证 + Release 产出 | 0.5–1 天 |
| **合计** | **约 3–5 天（不含踩坑缓冲）** |

## 8. 风险与回滚

| 风险 | 应对 |
| --- | --- |
| langchain/deepagents 动态导入打包后缺模块 | collect_all + 按验证清单逐项补 spec；打包机与干净机双验证 |
| 子进程 stdin/stdout 管道在 frozen 下异常 | acp_agent.exe console=True；首条验证（场景 3）优先 |
| 打包后路径错乱 | BASE_DIR 单点改造 + 场景 1/12 验证 |
| 杀软误报 | Release 说明 + 后续签名评估 |

回滚：全部改造通过 `FROZEN` 分支隔离，开发环境路径不变——出问题只影响打包产物，不影响开发运行；git revert 可整体回退。

## 9. 后续阶段（本期不做，架构预留）

- 认证登录（`docs/auth-login-plan.md`，另行评估）；
- Inno Setup 安装器（DATA_DIR 切 %APPDATA% + 数据迁移引导）；
- 代码签名、自动更新检查、CI（GitHub Actions）。
