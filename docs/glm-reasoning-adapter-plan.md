# GLM 推理模型 reasoning_content 适配方案（已探查，未实施）

> 状态：**方案已确认可行，暂未实施**（额度原因搁置）。实施时按本方案落地即可。
> 目标：GLM-4.6V-Flash 类推理模型回复在 `reasoning_content`、`content` 为空时，**把思考过程推给前端展示，但不进入模型上下文**。

## 问题背景

- 8088 端点（llama.cpp）加载 GLM-4.6V-Flash（推理模型）。
- 该模型的 OpenAI 兼容输出把内容放在 `reasoning_content` 字段，`content` 为空。
- langchain-openai 标准 `BaseChatOpenAI` **不提取** `reasoning_content`（官方 API 标准字段之外忽略）→ deepagents 拿到的消息 content 为空 → 流式对话无可见输出。

## 链路探查结论（已验证）

```
llama.cpp (SSE delta 带 reasoning_content)
  → langchain-openai 解析  ←← 断点①：_convert_delta_to_message_chunk 丢弃该字段
  → deepagents 主库（langchain agents middleware，消息透传，不重新规范化）
  → deepagents_acp server.py  ←← 已原生支持 reasoning
      _content_updates(): block type=="reasoning" → AgentThoughtChunk（agent_thought_chunk 事件）
  → 前端 ChatPage.vue（已有 thought/analysis 段渲染）
```

关键事实：

1. **deepagents_acp 协议层已现成**：`server.py` 的 `_content_updates()`（约 190 行）把 `{"type":"reasoning","reasoning":...}` 块转成 `AgentThoughtChunk`（ACP `agent_thought_chunk` 事件）流式推送；`_visible_reasoning()`（约 170 行）已实现。**无需改动**。
2. **langchain-core 1.6.9 已支持**：`AIMessage(content=[{"type":"reasoning","reasoning":"..."}])` 合法，`content_blocks` 属性存在（实测通过）。
3. **"不占上下文"天然成立**：langchain-openai `_convert_messages_to_dict`（base.py 约 411 行）在**发送给模型前**会丢弃 `reasoning_content`/`thinking` 类型块 → 思考过程只进 checkpoint 历史与前端展示，**不回送给模型**，不占模型上下文窗口。
4. **断点只有两处**（均在 langchain-openai `chat_models/base.py`，模块级函数）：
   - 流式：`_convert_delta_to_message_chunk`（约 554 行）只读 `_dict.get("content")`。
   - 非流式：`_convert_dict_to_message`（约 218 行）同样不读 `reasoning_content`。

## 实施方案（唯一改动文件：utils/model_util.py）

在 `create_model()` 中调用一次幂等的 `_install_reasoning_adapter()`，patch 上述两个模块函数：

```python
# utils/model_util.py 新增（代码草图，实施时按真实签名核对）
import langchain_openai.chat_models.base as _lc_base
from langchain_core.messages import AIMessage, AIMessageChunk

_ADAPTER_INSTALLED = False


def _with_reasoning_block(content, reasoning: str):
    """把 reasoning 文本并进消息 content（reasoning block 放在最前）。"""
    blocks = []
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        blocks = list(content)
    blocks.insert(0, {"type": "reasoning", "reasoning": reasoning})
    return blocks


def _install_reasoning_adapter() -> None:
    global _ADAPTER_INSTALLED
    if _ADAPTER_INSTALLED:
        return

    _orig_delta = _lc_base._convert_delta_to_message_chunk

    def _delta_with_reasoning(_dict, default_class):
        chunk = _orig_delta(_dict, default_class)
        reasoning = _dict.get("reasoning_content")
        if reasoning and isinstance(chunk, AIMessageChunk):
            chunk.content = _with_reasoning_block(chunk.content, reasoning)
        return chunk

    _orig_msg = _lc_base._convert_dict_to_message

    def _msg_with_reasoning(_dict):
        msg = _orig_msg(_dict)
        reasoning = _dict.get("reasoning_content") if isinstance(_dict, dict) else None
        if reasoning and isinstance(msg, AIMessage):
            msg.content = _with_reasoning_block(msg.content, reasoning)
        return msg

    _lc_base._convert_delta_to_message_chunk = _delta_with_reasoning
    _lc_base._convert_dict_to_message = _msg_with_reasoning
    _ADAPTER_INSTALLED = True
```

在 `create_model()` 返回前调用 `_install_reasoning_adapter()`。

## 验证方式（实施后必做）

1. **冒烟**：`create_model().invoke("你好")` → 断言 `msg.content_blocks` 含 `type=="reasoning"`；`astream` 逐 chunk 同理。
2. **不占上下文**：`_convert_message_to_dict(AIMessage(content=[reasoning block]))` → 输出 dict 不含 reasoning。
3. **真实链路**：Chat 模式发消息，抓 WS 事件应出现 `agent_thought_chunk`，前端显示思考段；随后正常出 text。
4. **回归**：agent 任务模式工具调用不受影响（工具调用在 `tool_calls` 字段，与 content 独立）。

## 已知权衡 / 未决点

- **展示内容 = 思维链**：GLM 的 reasoning_content 是思考过程而非最终答案，展示体验取决于模型；agent 任务模式下思考内容混入展示可能干扰判断 → 建议**仅 Chat 模式**启用 thought 展示（deepagents_acp 对子 agent 的 reasoning 默认 internal，主 agent 才外发，已天然隔离大部分）。
- **上下文使用率显示**：context_compactor 的 token 估算按 content 长度计算，可能把 reasoning 计入前端百分比 → 如需精确，后续在估算时排除 reasoning block。
- **回放顺序**：流式逐 chunk 事件顺序即展示顺序（reasoning 先到）；checkpoint 聚合后的 block 顺序可能因无 index 而乱序，仅影响历史回放的细微顺序，可接受。
- **patch 是全局的**：作用于进程内所有 ChatOpenAI 实例；本项目仅一个模型，副作用可控。
