# DeepAgents ACP 启动器 (app.exe)

一个用 **C# + [Photino.NET](https://www.tryphotino.io/)** 编写的轻量级桌面启动器。它会自动准备 Python 运行环境、启动 `app.py` 网关服务，并在一个内嵌的系统浏览器窗口（Windows 上为 WebView2 / Edge 内核）中打开页面。

## 功能

运行 `app.exe` 后，它会依次：

1. 检查项目根目录下的 `.venv`（要求 Python **3.12.x**）
2. 若不存在，用系统 Python 3.12 创建 `.venv` 并安装 `requirements.txt`（使用清华镜像）
3. 启动 `app.py`（监听 `http://127.0.0.1:8000`）
4. 等待服务就绪后，在窗口内自动打开页面
5. 关闭窗口时自动结束 `app.py` 进程

启动期间窗口内显示深色加载日志页；若系统缺少 Python 3.12，会显示带下载按钮的引导页。

## 使用

把发布目录里的两个文件放到**项目根目录**（与 `app.py`、`requirements.txt` 同级）后双击运行：

```
app.exe      # 主程序（自包含，无需安装 .NET 运行时）
app.ico      # 窗口标题栏图标
```

> 目标机器需要 Windows 自带的 **WebView2 Runtime**（Win10/11 基本已预装）。

## 从源码构建

需要 **.NET 8 SDK**。

```powershell
cd launcher
dotnet publish Launcher.csproj -c Release -o publish
```

产物在 `launcher/publish/`：`app.exe`（约 11 MB）+ `app.ico`。

### 重新生成图标

图标是 `</>` 代码符号风格，由脚本生成（需要时运行）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ..\.tmp\make_icon.ps1 -OutPath (Join-Path (Get-Location) 'app.ico')
```

## 关键配置（硬编码于源码）

| 项 | 值 | 位置 |
|---|---|---|
| 服务端口 | `8000` | `Program.cs` |
| 窗口标题 | `ACPAgent`（启动即最大化） | `Program.cs` |
| Python 版本要求 | `3.12.x` | `EnvRunner.cs` |
| pip 镜像源 | 清华 TUNA | `EnvRunner.cs` |
| Python 下载引导链接 | python.org 3.12.8 | `Program.cs` |

## 源码结构

| 文件 | 说明 |
|---|---|
| `Program.cs` | Photino 窗口、加载页/引导页、WebView ready 判定、生命周期 |
| `EnvRunner.cs` | `.venv` 检查/创建、依赖安装、启动 `app.py`、就绪探测 |
| `Launcher.csproj` | 项目配置（自包含、单文件、裁剪、图标） |
| `app.ico` | 应用图标 |

## 技术说明

- **为什么体积小**：Photino 复用操作系统自带的 WebView 内核，自身只有一层极薄的原生壳，因此支持裁剪（trimming），自包含单文件仅约 11 MB（对比 WPF/WinForms 自包含约 68 MB）。
- **WebView 时序**：加载页 DOM 就绪后通过 JS 回传 `ready` 消息，C# 收到后才进行后续的页面重载与导航，避免 Photino 在 Windows 上首帧未初始化时的原生内存访问错误。
