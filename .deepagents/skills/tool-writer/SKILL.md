---
name: tool-writer
description: 根据用户描述生成符合规范的 MCP 工具配置（server.json）。当用户要求"新建工具 / 接入 MCP 服务器 / 做一个 XX 工具"时使用，产出一个 stdio 启动规范、可直接落盘并试连的 server.json。
---

# 工具生成规范（Tool Writer）

当你被要求创建/接入一个新工具（MCP server）时，按本规范生成 `server.json` 配置，写入 `.deepagents/mcp_servers/<tool-name>/server.json`。

## 1. 命名规范

- 目录名 / `name` 字段必须一致，使用小写字母、数字、连字符（如 `filesystem-server`、`web-search`），最长 64 字符
- 名称体现工具职责，不要用通用词

## 2. server.json 结构（必须）

```json
{
  "name": "<tool-name>",
  "description": "<一句话描述：提供什么能力>",
  "command": "<启动命令>",
  "args": ["<参数1>", "<参数2>"],
  "cwd": "<可选：工作目录，缺省留空>",
  "env": {
    "<KEY>": "<VALUE 或 ${ENV_VAR} 占位>"
  }
}
```

字段说明：

- `command`：可执行程序或工具链。常见取值：`npx`（配合 args 指定包名，如 `npx -y @modelcontextprotocol/server-filesystem`）、`python`、`node`、系统已安装的二进制
- `args`：命令参数数组，每个参数一个元素，不要把整条命令写成一个字符串
- `cwd`：服务器工作目录，需要相对路径/项目内目录时必填；留空字符串或不填表示继承网关工作目录
- `env`：环境变量映射。支持 `${VAR}` 占位，运行时从进程环境读取（如 `"API_KEY": "${TAVILY_API_KEY}"`）；密钥类变量禁止把明文写进配置文件，一律用占位
- `description`：一句话说明工具能力，供 Agent 判断何时调用

## 3. 放置路径

- 唯一合法位置：`.deepagents/mcp_servers/<tool-name>/server.json`
- 生成后立即写入文件并检查 JSON 可解析

## 4. 交付前自检

- 命令是否真实存在（`npx` 类需确认包名正确；本地命令需确认已安装）
- 通过 `/api/manage/tools/<name>/test` 试连，确认能返回工具列表；试连失败时向用户说明错误原因，不要交付一个连不上的配置
- 生成后向用户汇报：工具名、路径、启动命令、一句能力说明

## 5. 禁止

- 不要生成不在 `.deepagents/mcp_servers/` 下的配置
- 不要把明文密钥/密码写进 server.json
- 不要覆盖已有工具配置（除非用户明确要求）
- 不要使用大写、空格、中文/特殊字符作工具名
