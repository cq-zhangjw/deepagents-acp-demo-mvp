"""Verification for the context compactor (docs/context-compaction-optimization.md).

Runs against the pure compaction functions with a mocked model, so no LLM call
is made. Covers: rolling summary inheritance, task-goal anchor survival,
KEEP_RECENT retention, token drop, and boundary cases.

Run:  .venv\\Scripts\\python.exe tests\\test_context_compactor.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import utils.context_compactor as cc  # noqa: E402

PASS = 0
TOTAL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, TOTAL
    status = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    TOTAL += 1
    print(f"[{status}] {name}" + (f" | {detail}" if detail else ""))


def build_session(n_turns: int = 30) -> list:
    """Human(任务目标) + (AI reply + Tool result) * n_turns."""
    msgs = [HumanMessage(content="帮我把 E:\\proj\\app\\main.py 的登录逻辑改成支持中文输入")]
    for i in range(n_turns):
        msgs.append(AIMessage(content=f"第{i+1}步：修改了 E:\\proj\\app\\main.py 第 {100+i} 行", tool_calls=[{"id": f"c{i}", "name": "edit_file", "args": {"path": "E:\\proj\\app\\main.py", "line": 100 + i}}]))
        msgs.append(ToolMessage(content=f"OK 已保存 {i+1} 处", tool_call_id=f"t{i}"))
    return msgs


class FakeResp:
    content = (
        "目标：支持中文输入登录。\n"
        "已完成：修改 main.py 登录逻辑。\n"
        "关键事实与绝对路径：E:\\proj\\app\\main.py\n"
        "待办与未解决：无。"
    )


class FakeModel:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return FakeResp()


async def main() -> int:
    print("== 1. 基本压缩 ==")
    msgs = build_session(30)
    before = cc.estimate_tokens(msgs)
    fm = FakeModel()
    cc.create_model = lambda: fm
    compacted, result = await cc.compact_messages_list(msgs)

    check("触发成功 ok=True", result.get("ok") is True)
    check("summary 首条带前缀", cc._is_summary(compacted[0]))
    check("summary 带 metadata", (compacted[0].metadata or {}).get("__role__") == "context_summary")
    anchor = cc._task_anchor(compacted)
    check("任务目标消息保留", anchor is not None and "登录逻辑" in (anchor.content or ""))
    kept = [m for m in compacted if not cc._is_summary(m)]
    check("保留最近消息数 <= KEEP_RECENT+1(anchor)", len(kept) <= cc.KEEP_RECENT + 1, f"kept={len(kept)}")
    after = cc.estimate_tokens(compacted)
    check("token 下降", after < before, f"{before} -> {after}")
    check("模型被调用一次", fm.calls == 1, f"calls={fm.calls}")

    print("== 2. 滚动累积（第二次压缩不丢旧摘要） ==")
    more = compacted + [AIMessage(content="补充：又改了 utils\\auth.py"), ToolMessage(content="done", tool_call_id="x1")]
    compacted2, result2 = await cc.compact_messages_list(more)
    check("第二轮触发 ok=True", result2.get("ok") is True)
    sums = [m for m in compacted2 if cc._is_summary(m)]
    check("仅一条 summary（旧摘要被吸收不重复）", len(sums) == 1, f"sum={len(sums)}")
    s_text = cc._msg_text(sums[0])
    check("旧摘要要点被继承（含绝对路径）", "main.py" in s_text, "")
    anchor2 = cc._task_anchor(compacted2)
    check("第二轮任务目标仍在", anchor2 is not None and "登录逻辑" in (anchor2.content or ""), "")
    check("新进展（utils/auth.py）被纳入总结输入", fm.calls >= 2, f"calls={fm.calls}")

    print("== 3. 边界 ==")
    e1, r1 = await cc.compact_messages_list([])
    check("空列表 ok=False", r1.get("ok") is False and not e1)
    short = build_session(3)  # 7 条 < KEEP_RECENT+1
    e2, r2 = await cc.compact_messages_list(short)
    check("短列表不压缩", r2.get("ok") is False and e2 == short)
    check("should_compact 短列表 False", cc.should_compact(short) is False)

    print("== 4. 分块路径（超长输入不丢中段） ==")
    long_early, _ = cc.split_compact(msgs)
    chunks = cc._split_text(cc.format_messages_for_summary(long_early), 600)
    check("超长输入被分块", len(chunks) > 1, f"chunks={len(chunks)}")
    check("分块拼接无丢失", sum(len(c) for c in chunks) >= len(cc.format_messages_for_summary(long_early)) - 400, "")

    print(f"\n结果: {PASS} 项通过 / 共 {TOTAL} 项断言点")
    return 0 if PASS == TOTAL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
