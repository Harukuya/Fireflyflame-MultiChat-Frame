"""
判断模型（这边新加的）：群里「要不要接话、感不感兴趣」。用设置里选的判断模型，不选就和她说话用同一个。

交的是理由，不是一个分数（心流插件是加权打分过阈值；这里让模型直接说要不要、为什么，理由记进仪表盘）。
⭐ 系统提示词由 group.py 拼好交进来：接话窗口是一段短的（只看要不要接着说）；
  看手机是她自己的完整提示词（身份、她记得的、在场的人、悬着的事）+ 判断那一段，按她真的在不在意来判断。
看手机那一次可以「多看几条」：每次往前读 10～15 条，最多 50 条（不重新检索）。
"""

from __future__ import annotations

from . import profile

import math
import random

from .caption import image_note
from .quotes import render_quote
from .jsutil import message_time
from .winext import JUDGE, READ_MORE

MAX_READ = 50


def _lines(msgs: list[dict], trailblazer: str = "") -> str:
    def who(m: dict) -> str:
        name = m.get("name") or "某人"
        return f"{name}·主要联系人" if trailblazer and m.get("uid") == trailblazer else name
    return "\n".join(f"#{i + 1} {message_time(m.get('ts'))} 【{who(m)}】{'→' + profile.bot_name() if m.get('called') and not m.get('read') else ''}"
                     f"（{'本群前文' if m.get('read') else '本轮新看到的消息'}）："
                     f"{render_quote(m, msgs)}{image_note(m.get('attachment')) + m['text'] or ('［未附加文字］' if m.get('quote') else '［图片］')}"
                     if m["role"] == "user" else f"#{i + 1} {message_time(m.get('ts'))} 【{profile.bot_name()}】（本群前文）：{m['text'] or '［表情］'}" for i, m in enumerate(msgs))


async def judge(llm, system: str, mode: str, pool: list[dict], start: int, trailblazer: str = "", count_cb=None,
                prepare=None, gate=None, system_for=None) -> dict:
    """
    pool：这个群可以读到的消息（按时间）；start：这次从哪一条开始读（之后的都看）。
    prepare：每次交给判断模型之前，对这次要看的消息做的准备（转述图片）。
    gate：准备完、真要问判断模型的那一刻还能不能问（精力）；不能 → 返回 skipped（没看，什么都不算看过）。
    返回 {"reply", "interest", "reason", "focus"（消息 id 列表）, "to"（某个人 / 话题）, "read"（这次看了的消息）}。
    判断失败：被叫到就回，不然不回。
    """
    can_more = mode == "look"
    def failed(msgs, detail):
        focus = [m["id"] for m in msgs if m.get("called") and not m.get("read")]
        return {"reply": bool(focus), "interest": 0.5 if focus else 0,
                "reason": f"判断没成功（{detail}）", "focus": focus, "to": "", "read": msgs, "remember": True,
                "read_completed": False}

    while True:
        msgs = pool[start:]
        if prepare is not None:
            await prepare(msgs)
        if gate is not None and not gate():
            return {"reply": False, "interest": 0, "reason": "累了，没看", "focus": [], "to": "", "read": [], "skipped": True,
                    "read_completed": False}
        tools = [JUDGE, READ_MORE] if can_more and start > 0 and len(msgs) < MAX_READ else [JUDGE]
        if count_cb:
            count_cb()
        # 多看几条后只刷新在场者及其认知；检索背景由调用者缓存，不再查一次。
        prompt = system_for(msgs) if system_for is not None else system
        prompt += ("\n\n【本次判断的消息】\n"
                   "**本轮新看到的消息是眼前要判断的内容；本群前文只帮助理解之前聊了什么。**\n"
                   "前文里以前叫过你的话不是又叫了一次；旧记事和回想也不是新收到的群消息。")
        r = await llm.chat_with_retry(prompt, [{"role": "user", "content": _lines(msgs, trailblazer)}], tools, "required")
        if not r.ok or not r.calls:
            return failed(msgs, "缺少必需工具结果" if r.kind == "missing-tool" else r.kind or "没交结果")
        calls = [c for c in r.calls if isinstance(c, dict)]
        if any(c.get("name") == "read_more" for c in calls) and "read_more" in [t["name"] for t in tools]:
            start = max(0, start - random.randint(10, 15), len(pool) - MAX_READ)
            continue
        c = next((c for c in calls if c.get("name") == "judge"), None)
        if not c:
            return failed(msgs, "没交结果")
        inp = c.get("input")
        try:
            finite_interest = (isinstance(inp, dict) and type(inp.get("interest")) in (int, float)
                               and math.isfinite(inp["interest"]))
        except (OverflowError, ValueError):
            finite_interest = False
        if (not isinstance(inp, dict) or type(inp.get("reply")) is not bool
                or not finite_interest or not isinstance(inp.get("reason"), str)
                or not isinstance(inp.get("focus", []), list)
                or any(type(n) is not int or not 1 <= n <= len(msgs) for n in inp.get("focus", []))
                or ("to" in inp and inp["to"] not in ("某个人", "话题"))):
            return failed(msgs, "结果格式不对")
        interest = max(0.0, min(1.0, float(inp["interest"])))
        focus = []
        for n in inp.get("focus") or []:
            if isinstance(n, int) and 1 <= n <= len(msgs):
                focus.append(msgs[n - 1]["id"])
        to = inp.get("to") if inp.get("to") in ("某个人", "话题") else ""
        return {"reply": inp.get("reply") is True, "interest": interest, "reason": str(inp.get("reason") or "")[:80],
                "focus": focus, "to": to, "read": msgs, "read_completed": True,
                "remember": inp.get("remember") if type(inp.get("remember")) is bool else True}

