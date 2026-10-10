---
name: tool-creator
description: 根据用户描述，经过交互确认后生成符合规范的 MCP 工具代码与配置。当用户要求"新建工具 / 接入 MCP 服务器 / 做一个 XX 工具"时使用，完整执行需求确认→编码→导出 manifest.json 的全流程。
---

# 工具生成规范（Tool Creator）

## 整体流程

### 第一步：需求收集与设计确认

当用户描述想要的工具功能后，**先不要编码**，而是设计并展示以下信息供用户确认：

| 项目 | 内容 |
|------|------|
| MCP 服务名 | `<服务名>`（小写字母、数字、下划线，如 `calc_server`） |
| 工具函数名 | `<工具名>`（动词/名词，如 `calculate`） |
| 参数列表 | 参数名、类型、是否必填、说明 |
| 返回值 | 返回类型与说明 |
| 功能概要 | 一句话描述工具能力 |

若用户有修改意见，则调整上述设计并重新展示，直到用户明确确认后再进入下一步。

---

### 第二步：创建目录与 MCP Server 代码文件

目录路径：`.deepagents/tools/mcp_servers/<服务名>/`
文件路径：`.deepagents/tools/mcp_servers/<服务名>/<服务名>_mcp_server.py`

代码模板（严格遵守以下结构）：

```python
import traceback
from fastmcp import FastMCP

mcp = FastMCP("<服务显示名>")


@mcp.tool()
def <工具名>(<参数1>: <类型>, <参数2>: <类型>, ...) -> <返回值类型>:
    """
    <方法功能描述>

    Args:
        <参数1>: <参数1的描述和示例>
        <参数2>: <参数2的描述和示例>
        ...

    Returns:
        <返回值描述>
    """
    try:
        <实现逻辑>
    except:
        raise Exception(traceback.format_exc())


if __name__ == "__main__":
    import sys
    sys.stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)
    sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)
    mcp.run(transport="stdio")
```

**代码规范（必须遵守）**：
- **全局禁止 `print()` 语句**，print 输出会污染 stdio 通信导致工具不可用
- 异常必须通过 `raise Exception(traceback.format_exc())` 抛出，不要静默忽略
- 入口代码固定使用上述 `if __name__ == "__main__":` 模板，确保 stdio 编码正确

---

### 第三步：导出 manifest.json

编码完成后，在 **MCP 服务所在目录**执行以下命令导出配置：

```powershell
cd .deepagents/tools/mcp_servers/<服务名>
fastmcp inspect "<服务名>_mcp_server.py" --format mcp -o "manifest.json"
```

- 若命令执行失败，分析错误原因，修正代码后重新执行，直到成功为止
- 成功后确认 `manifest.json` 文件存在且内容可解析

---

### 第四步：向用户报告结果

报告内容包括：
- MCP 服务名与工具名
- 文件路径（服务文件 + manifest.json）
- 工具能力说明
- 是否需要额外依赖包（如有，列出安装命令）

---

## 命名规范

- 服务目录名：小写字母、数字、下划线，最长 64 字符（如 `calc_server`、`file_reader`）
- 工具函数名：小写字母、下划线，动词/名词风格（如 `calculate`、`read_file`）
- **禁止**使用大写、空格、中文、特殊字符

## 禁止事项

- 编码前必须完成需求确认，不允许跳过确认步骤直接编码
- 文件只能放置于 `.deepagents/tools/mcp_servers/<服务名>/` 下
- 不要把明文密钥/密码写入任何配置文件
- 不要覆盖已有工具配置（除非用户明确要求）
- 代码中严禁使用 `print()` 语句
