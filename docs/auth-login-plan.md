# 认证登录方案（单用户密码登录）

日期：2026-10-17
状态：方案评估（未实施）；用户确认后实施
适用：当前桌面/局域网工具定位，防止未授权访问（局域网窥探、未来公网暴露的前置条件）

## 现状（已核查）

- 后端 FastAPI（app.py）：`/api/*` 十余组路由（history/groups/todos/knowledge/manage/context/attachments/preview/reveal/timers/voice）**全部无鉴权**。
- `/acp-ws` WebSocket：浏览器直连，仅 `?agent=` 参数，**无身份校验**。
- `/uploads` 静态挂载原始附件；`/` 挂载 web/dist（SPA 壳）。
- 前端：vue-router hash 模式（`/` 聊天页、`/voice` 语音页）；API 用原生 `fetch` 散落几十处；ws 用原生 `WebSocket(ws://host/acp-ws?agent=xx)`。

## 目标

- 单用户、密码登录；未登录无法访问任何数据 API、WebSocket、附件。
- 轻量、纯增量、不影响现有功能；支持局域网使用（默认 http，公网时需 HTTPS）。

## 方案

### 后端（新增 `api/auth.py`，约 120-160 行）

1. **凭据配置**
   - 优先读环境变量 `APP_PASSWORD`；
   - 未设置时首次启动生成 12 位随机密码，以 SHA-256 加盐摘要存入 `db/auth_secret.json`，并在启动日志打印一次明文（用户保存后可删除）。
2. **接口**
   - `POST /api/auth/login {password}`：恒定时间比较 → 签发 `secrets.token_urlsafe(32)` 会话 token（内存 dict，可配 `APP_AUTH_TTL_HOURS`，默认 24h）→ 返回 `{token, expiresAt}`。
   - `POST /api/auth/logout`：吊销当前 token。
   - `GET /api/auth/status`：探测当前是否已登录（供前端启动校验），返回 `{authenticated}`。
   - 登录失败限流：同 IP 连续失败 5 次锁定 10 分钟（内存计数，可选但建议做，约 20 行）。
3. **鉴权中间件**（FastAPI middleware，挂 app.py）
   - 白名单：`POST /api/auth/login`、`GET /api/auth/status`、`/`（SPA 壳）、`/assets/*`、`/favicon.ico`。
   - 其余 **全部**（含 `/api/*`、`/uploads/*`、`/acp-ws`）校验：请求头 `Authorization: Bearer <token>` **或** Cookie `acp_session=<token>`。
   - 未登录：HTTP API 返回 401 JSON；`/uploads` 返回 401/403；ws 拒绝连接 `close(1008)`。
   - **推荐 header 优先**（前端统一带 header），天然规避 CSRF（跨站请求无法携带自定义头）。
4. **WebSocket 鉴权**
   - 优先读 `websocket.cookies["acp_session"]`（同源 WebSocket 自动带 cookie），其次 `?token=`。
   - 非法连接在 `accept()` 前 `close(code=1008, reason="unauthorized")`。
5. **其他**
   - token 吊销集（logout/超时）内存维护；服务重启后全部失效（重新登录）。
   - `uploads` 纳入保护（附件属隐私数据）。

### 前端（新增 `LoginPage.vue` + `auth.ts`，改造 `main.ts`/`router`/ws 一处，约 150-220 行）

1. **`auth.ts`**：登录/登出/status 封装 + token 存 `localStorage`（key 如 `acp_token`）。
2. **`main.ts` 全局 fetch 包装**：patch 原生 `window.fetch`——自动附加 `Authorization: Bearer <token>`；响应 401 时清 token 并 `location.hash = '#/login'`。
   - **这是关键决策**：不改散落几十处的 `fetch('/api/...')` 调用点，侵入最小、回归风险最低。
3. **`LoginPage.vue`**（路由 `/login`）：密码框 + 登录按钮 + 错误提示 + 三语文案；成功后 `router.replace` 回原页面。
4. **路由守卫**：`router.beforeEach`——目标非 `/login` 且本地无 token 时跳 `/login`；`/login` 已有 token 跳 `/`。
5. **ws 连接**：`websocketUrl()` 追加 `&token=<token>`（后端优先 cookie，双保险）。
6. **App.vue**：不动；登录态初始化在 `main.ts`（`auth.init()` 探测一次 status）。

### 配置与文档

- 新增环境变量：`APP_PASSWORD`（首选）、`APP_AUTH_TTL_HOURS`（默认 24）。
- README / docs 补充：启用方式、随机密码找回（重启重新生成）、公网必须 HTTPS 的提醒。

## 工作量评估

| 项 | 估计 |
|---|---|
| 后端 auth.py + 中间件 + ws/uploads 保护 | 0.5 - 1 天 |
| 前端 LoginPage + 守卫 + fetch 包装 + ws token | 0.5 - 1 天 |
| 测试（401 拦截、登录流程、ws 拒绝、超时、限流、记住我） | 约 0.5 天 |
| **合计** | **1.5 - 2.5 天** |

## 对现有功能的影响

- 纯增量：中间件 + 前端守卫 + fetch 包装；不登录无法使用（行为变化仅在"必须登录"）。
- 后端 ws/API 协议不变；agent 执行链路、定时任务、语音通话不受影响（登录态贯穿同一会话）。
- 风险点：fetch 包装若漏处理 multipart/事件流等特殊请求需逐一核对（附件上传、SSE 路径）；测试覆盖即可。

## 安全要点

- 密码不明文存：SHA-256 + 随机盐。
- 会话 token 256 位随机；header 优先（防 CSRF）。
- 登录限流防爆破；token 24h 过期 + 登出吊销。
- 公网使用必须 HTTPS（cloudflared 等），否则密码/指令明文可被截获。
- 局域网默认 http 可接受；强烈建议首次使用设置 `APP_PASSWORD`。

## 验证方案

1. 未登录：`GET /api/history` → 401；`/acp-ws` 连接 → 1008。
2. 登录：错误密码 → 401（5 次后限流）；正确 → token；带 token 访问全部 API 正常。
3. 前端：未登录访问 `/` → 跳 `/login` → 登录 → 回原页；ws 连通；语音页同规则。
4. 登出/超时：token 吊销后 401 → 自动跳登录。
5. 回归：登录后现有功能（会话、分组、定时任务、附件、语音）逐项冒烟。

## 回滚

- 新增文件（auth.py/LoginPage.vue/auth.ts）git revert 即回退；中间件一行关闭；fetch 包装删除。
- 不触碰会话/分组/定时任务数据。
