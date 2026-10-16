# 上下文压缩优化 —— 实施方案

> 状态：**实施前方案存档**（本文件先于代码改动保存，供评审与回滚参照）
> 适用版本：当前 main（表权威会话上下文：`RollbackAgentServer` + `utils/context_compactor.py`）
> 目标：修复"聊着聊着，上下文压缩后丢失关键信息 / 继续任务时忘记任务"的问题，同时**不影响现有主体功能**。

---

## 1. 现状与问题（已核查代码）

实际生效链路（`utils/context_compactor.py` + `api/context.py` + `acp_agent.py::RollbackAgentServer`）：

```
触发（estimate_tokens + COMPACT_OVERHEAD(6000) >= CONTENT_SIZE(32768) * 0.85）
→ split_compact：除最近 KEEP_RECENT=8 条外，其余消息全部删除
→ build_summary：把 early 消息渲染为文本；超长（> ~33423 字符）时只保留 head+tail
                 （中段用 [... omitted ...] 代替），再让模型总结（≤2048 tokens）
→ compacted = [HumanMessage(summary), *recent]
→ 写回 session-context 表；下次 prompt 用表内容重新播种 checkpoint 后执行
```

已确认的 5 个问题（按严重度）：

| # | 问题 | 后果 |
| --- | --- | --- |
| 1 | **无滚动累积**：每次压缩都对"早期全部消息（含上一次的 summary）"重新总结 | 摘要逐轮衰减（传话游戏效应），聊得越久任务细节丢得越多 —— **忘记任务的主因** |
| 2 | **任务目标消息被一刀切删除**：第一条用户消息属于 early 部分 | 原始任务描述只剩模型转述，目标细节不可靠 |
| 3 | **工具结果丢失**：读过的文件内容 / 绝对路径 / grep 结果（ToolMessage）全部删除，summary 为自由文本 | agent 失去"工作记忆"，继续任务时不知道之前在哪改、看到了什么 |
| 4 | **总结输入超长截断丢中段**：head+tail 各半，中段决策被省略 | 总结模型看不到任务执行中段的关键决策 |
| 5 | **触发偏晚 + 保留太少**：chars/1.7 低估 token；KEEP_RECENT=8 太少 | 压缩发生在窗口快爆时，留给总结的空间不足；近期上下文保留不足 |

---

## 2. 优化方案设计

### P0 —— 直接解决"忘记任务"

**① 滚动累积摘要（杜绝逐轮衰减）**
- 给 summary 消息打角色标记：`metadata={"__role__": "context_summary"}` + content 前缀 `【历史上下文摘要】`（双保险，兼容表存储丢 metadata 的情况）；
- 压缩时识别旧 summary 消息：**永不删除**，并作为总结输入**完整保留**（不参与截断）；
- `build_summary` 的 prompt 增加指令："以下是之前的对话摘要，必须完整保留其中所有要点，只对新增对话内容进行压缩"；
- 结果：每次压缩 = 旧摘要（不动） + 新段压缩 → 信息单调累积，不再衰减。

**② 任务目标锚点**
- 识别会话中**第一条用户消息**（非 summary、最早的 HumanMessage）作为任务目标；
- 压缩后**原样保留**在 summary 之后（`compacted = [summary, 任务目标, *recent]`）；
- 效果：无论压缩多少轮，模型都能看到最初的任务描述原文。

**③ 结构化总结模板**
- summary prompt 输出固定结构，强制保留关键事实：
  ```
  目标：<原始任务目标与约束>
  已完成：<步骤与结果>
  当前进度：<进行到哪一步>
  关键事实与绝对路径：<必须列出所有文件路径 / 数据 / 结论，漏列视为失败>
  已做决策：<影响后续的决策>
  待办与未解决：<未完成事项 / 正在排查的错误>
  ```
- 解决"忘了在哪干活"（路径/决策强制列全）。

### P1 —— 增强

**④ KEEP_RECENT 8 → 12**：保留更多近期上下文（最近 12 条消息原样保留）。

**⑤ 角色清晰**：summary 消息带前缀与 metadata，模型明确知道它是历史摘要而非新用户消息。

### P2 —— 稳健性

**⑥ 分块总结替代 head+tail 截断**
- 当总结输入超长时：**旧 summary 单独完整保留**；其余新内容按块（每块 ≤ 输入上限）**逐块总结后合并**，避免中段丢失；
- 实现：`build_summary` 内对超长文本按 `\n\n` 边界切块，逐块 `MODEL.ainvoke`，再合并。

**⑦ token 估算修正（可选）**
- `estimate_tokens` 保持 chars/1.7（与前端口径一致，不动，避免前端显示跳动）；
- 仅将压缩触发阈值微调（`COMPACT_RATIO` 0.85→0.8），让压缩更从容（改 env 默认值，可通过 .env 覆盖）。

---

## 3. 改动清单

| 文件 | 改动 |
| --- | --- |
| `utils/context_compactor.py` | ①⑤ 角色标记 + 滚动累积；② 任务目标锚点；③ 结构化模板；④ KEEP_RECENT=12；⑥ 分块总结；⑦ 默认阈值 0.80 |
| `api/context.py` | **不改**（调用 `compact_messages_list`，签名不变，自动获得新行为） |
| `acp_agent.py` | **不改**（`RollbackAgentServer`/`CompactingAgentServer` 均调用 compactor，签名不变） |
| `.env` | **不改**（仅改代码默认值，用户 .env 覆盖仍优先） |
| `web/src/pages/ChatPage.vue` | 仅侧栏布局调整（见第 5 节，独立于压缩改动） |

兼容性：
- `compact_messages_list(messages) -> (list, dict)` 签名不变 → api/context.py、acp_agent.py 两个调用方零改动；
- summary 消息用 HumanMessage（与原实现一致），仅增加前缀与 metadata；表存储 / checkpoint replay 均按 BaseMessage 序列化，兼容；
- 分块总结失败时回退为单块（不截断语义变化）。

---

## 4. 验证方案

1. **单元验证（压缩逻辑，不起服务）**：构造模拟消息列表（目标 + 多轮工具调用 + 长内容），调 `compact_messages_list`：
   - 断言：返回值首条为 summary（带前缀/元数据）；任务目标消息仍在；KEEP_RECENT 生效（保留最近 12 条）；token 下降；
   - 再次对压缩结果调用（模拟第二轮压缩）：断言**旧 summary 未删、内容未被重写丢失**（含前缀）；
2. **回归**：`python -c` 冒烟跑通 `should_compact` / `estimate_tokens` 边界（空列表、短列表不触发）；
3. **端到端（可选）**：重启后端 → 打开已有长会话 → 手动 `POST /api/context/compact` → 前端上下文百分比下降、消息继续正常发送。
4. **侧栏布局**：Playwright 1600x950 打开 → 构造 20+ 会话 → 断言上方区块（新建会话/定时任务/Agent 管理/搜索）完整可见、会话列表区域出现内部滚动（scrollHeight > clientHeight）且列表项完整渲染在容器内。

---

## 5. 侧栏布局调整（独立小改动）

问题：会话变多时列表把上方空间挤掉/列表溢出视口被裁。

改动（`ChatPage.vue` CSS）：
- `.chat-app`：`height:100vh` 兜底 + `height:100dvh`（防止 dvh 不支持时高度失效）；
- `.sidebar`：加 `height:100%; min-height:0`（明确高度约束）；
- `.conversation-list`：`flex:1 1 auto` → `flex:1 1 0`（basis 归零，强制吃满剩余空间并内部滚动）；
- 上方区块压缩留白，给列表更多空间：`.brand-row` padding 23px→14px、`.sidebar-nav` margin-bottom 14px→10px、`.new-conversation` margin-bottom 16px→12px、`.conversation-search` padding 8px→6px、`.conversation-label` padding 8px→6px。

---

## 6. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 压缩逻辑改动影响 prompt 流程 | 签名不变、异常兜底（`except: 继续不压缩` 已有），P0 改动集中在一个文件 |
| 任务目标锚点重复（目标消息本就在 recent 内） | 判断锚点不在 recent 才加入，避免重复 |
| summary 识别误判（用户消息恰好带前缀） | 前缀带特殊标记（`【历史上下文摘要】`），metadata 为主判据，前缀为兜底 |
| 分块总结变慢 | 仅超长才分块；每块上限可配置 |

回滚：`utils/context_compactor.py` 单文件 revert；侧栏改动单独提交可独立 revert。
