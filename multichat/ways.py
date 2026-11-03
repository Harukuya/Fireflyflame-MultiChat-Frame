"""
回他的方式（原版 ways.ts，2026-10-29 作者定）

⭐ 她回话不总是一个样：照常回复、追问、分享、索取、逗他、收着、岔开。
  怎么回由她自己选，回完在「这轮的感受」里报一下用了哪种（tools.py 的 round_feeling）。
⭐ 程序只按状态限制、做平衡：
  - 心情是她自己的情绪，不据此禁止向主要联系人索取、逗他等亲近方式
  - 连着两轮都在问他、向他要东西，下一轮告诉她一声（禁则 4「你不是在采访他」）
⭐ 原版存在浏览器的 localStorage（只有一个对话）；这里每个会话各存一份，放在 state.json（store.extra["ways"]）。

这边新加的（原版没有）：
  - 群友私聊：不给「索取」（向群友撒娇、要他在乎不合适）；也会感受她自己的心情
  - 群聊：去掉「索取」，其余几种换成群里的说法；「逗他」叫「开玩笑」
"""

from __future__ import annotations

from .jsutil import MIN, now_ms

# 七种方式：叫法（工具参数里用中文，她读得最顺）和一句说明——原文
WAYS = [
    {"id": "normal", "label": "照常回复", "desc": "接住他的话，说你的看法"},
    {"id": "ask", "label": "追问", "desc": "当场问你想知道的"},
    {"id": "share", "label": "分享", "desc": "说说你自己这边的事、你想到的"},
    {"id": "want", "label": "索取", "desc": "向他要点什么：想听他多说说、想要他的看法、想让他陪你一会儿，或者想被他在乎、撒个娇"},
    {"id": "tease", "label": "逗他", "desc": "开个玩笑，或者适度模仿熟悉人物的语气"},
    {"id": "brief", "label": "收着", "desc": "只简单回一两句：累了、心情淡，或者这件事不想深谈"},
    {"id": "dodge", "label": "岔开", "desc": "不想谈的事，笑着带过去"},
]

# 群里（这边定的）：不索取；说法换成对着大家
GROUP_WAYS = [
    {"id": "normal", "label": "照常回复", "desc": "接住大家的话，说你的看法"},
    {"id": "ask", "label": "追问", "desc": "当场问你想知道的"},
    {"id": "share", "label": "分享", "desc": "说说你自己这边的事、你想到的"},
    {"id": "tease", "label": "开玩笑", "desc": "接个梗、开个玩笑，或者适度模仿熟悉人物的语气"},
    {"id": "brief", "label": "收着", "desc": "只简单回一两句：累了，或者这件事不想深谈"},
    {"id": "dodge", "label": "岔开", "desc": "不想谈的事，笑着带过去（比如有人问起私聊里的事）"},
]


def group_way_note(ways: list[dict]) -> str:
    listed = "；".join(f"{w['label']} = {w['desc']}" for w in ways)
    return f"这轮你是怎么接的：{listed}。不必每次都照常回复，看你想怎么接；别连着几轮都在问别人。"


def available_ways(mood_value: float) -> list[dict]:
    """主要联系人私聊保留全部方式；她自己的低落不等于与主要联系人疏远。"""
    return list(WAYS)


def peer_ways() -> list[dict]:
    """群友私聊：不索取"""
    return [w for w in WAYS if w["id"] != "want"]


# 隔这么久没聊，就重新算「连着几轮」
SESSION_GAP = 30 * MIN
# 连着几轮都在问他、向他要东西，就告诉她一声
ASKING_STREAK = 2
KEEP = 6


def note_way(store, way: str | None, now: int | None = None) -> None:
    """回完一轮：记下她用的方式（没报就当照常回复），最多存 6 条"""
    now = now_ms() if now is None else now
    store.extra["ways"] = [*store.extra.get("ways", []), {"way": way or "normal", "at": now}][-KEEP:]
    store.save_state()


def asking_streak(store, now: int | None = None) -> int:
    """最近连着几轮都在问他、向他要东西（隔 30 分钟以上没聊就断了）"""
    t = now_ms() if now is None else now
    n = 0
    for e in reversed(store.extra.get("ways", [])):
        if t - e["at"] > SESSION_GAP:
            break
        if e["way"] not in ("ask", "want"):
            break
        n += 1
        t = e["at"]
    return n


def way_of_label(label) -> str | None:
    return next((w["id"] for w in (*WAYS, *GROUP_WAYS) if w["label"] == label), None)
