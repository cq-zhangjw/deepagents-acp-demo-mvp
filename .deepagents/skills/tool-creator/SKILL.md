---
name: tool-creator
description: 根据用户描述生成符合规范的 MCP 工具配置。当用户要求"新建工具 / 接入 MCP 服务器 / 做一个 XX 工具"时使用，产出一个含 manifest.json（工具清单）的可落盘工具目录；server.json 仅作为可选启动配置。
---

# 工具生成规范（Tool Creator）

当你被要求创建/接入一个新工具（MCP server）时，按本规范在 `.deepagents/tools/mcp_servers/<tool-name>/` 下生成 `manifest.json`（必须）与 `server.json`（可选启动配置）。

## 1. 命名规范

- 目录名使用小写字母、数字、连字符（如 `filesystem-server`、`calc_server`），最长 64 字符
- 名称体现工具职责，不要用通用词

## 2. manifest.json 结构（必须）

```json
{
  "serverInfo": {
    "name": "<工具名>",
    "version": "1.0.0"
  },
  "tools": [
    {
      "name": "<tool-name>",
      "description": "<一句话描述：提供什么能力>",
      "inputSchema": {
        "type": "object",
        "properties": {
          "<参数名>": { "type": "string", "description": "<参数说明>" }
        },
        "required": ["<必填参数名>"]
      }
    }
  ]
}
```

- `tools[].name`：Agent 实际调用时的工具名，用动词/名词命名（如 `calculate`）
- `tools[].description`：说明工具能力与参数，供 Agent 判断何时调用
- `tools[].inputSchema`：符合 JSON Schema 的入参定义（type/properties/required）

## 3. server.json（可选启动配置）

仅当工具需要外部进程启动时才提供；纯函数型工具（如本地计算）可省略：

```json
{
  "name": "<tool-name>",
  "command": "<启动命令>",
  "args": ["<参数1>", "<参数2>"],
  "cwd": "<可选：工作目录>",
  "env": { "<KEY>": "<VALUE 或 ${ENV_VAR} 占位>" }
}
```

- `command`：常见取值 `npx`（配合 args 指定包名）、`python`、`node`、系统已安装二进制
- `env`：支持 `${VAR}` 占位，运行时从进程环境读取；密钥类变量禁止明文，一律占位

## 4. 放置路径

- 唯一合法位置：`.deepagents/tools/mcp_servers/<tool-name>/`（`manifest.json` 必须，`server.json` 可选）
- 生成后立即写入文件并检查 JSON 可解析；工具启用后其信息会聚合进根目录 `tools.json`（由管理面板自动维护，无需手工编辑）

## 5. 交付前自检

- 工具名称是否规范、manifest.json 是否可解析、inputSchema 是否完整
- 提供 server.json 时确认命令真实存在，并通过 `/api/manage/tools/<name>/test` 试连
- 生成后向用户汇报：工具名、路径、能力说明

## 6. 禁止

- 不要生成不在 `.deepagents/mcp_servers/` 下的配置
- 不要把明文密钥/密码写进 server.json
- 不要覆盖已有工具配置（除非用户明确要求）
- 不要使用大写、空格、中文/特殊字符作工具名
