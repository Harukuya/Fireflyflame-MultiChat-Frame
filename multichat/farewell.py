"""每个会话自己的道别收尾。只保存确实送出的状态，不改变全局作息。"""

from __future__ import annotations

from . import profile

import math
import re

from .jsutil import HOUR, message_time, now_ms
from .energy import apply_round
from . import activity, persona
from .caption import image_note
from .quotes import render_quote
from .sanitize import to_messages
from .winext import time_block

FOLLOWUP_TIMEOUT_S = 20.0


def followup_system(identity: str, relationship: str = "", *, now: int | None = None, state_fn=None) -> str:
    """最后一次收尾只用角色语气、眼前的话和本地人际认知，不检索任何资料。"""
    now = now_ms() if now is None else now
    return "\n\n".join(s for s in [
        profile.prompt("identity") + "\n" + identity,
        persona.VOICE,
        time_block(now),
        activity.current_context(now, state_fn=state_fn),
        "【这次和谁说话】\n" + relationship if relationship else "",
        "【道别后的最后一次回应】\n"
        "**你刚才已经道别，现在确实得下线了。这是本次聊天最后的一条回复。**\n"
        "结合对方这次的话，温和地接住，再自然表达歉意和确实得先离开的意思。"
        "不用重复整段道别，沿用刚才说过的原因；不知道原因就只说得先走，不编新安排。\n"
        "只写一条简短的聊天消息，一两句话即可。不要展开新话题、反问、索取、留新约定，"
        "不要许诺会马上回来，也不用提到规则、模型或剩余次数。\n"
        "【下面的对话】\n"
        "**标为「这次要接的话」的才是本次要回复的；前文只帮助衔接，不是风格范例。**\n"
        "只依据这里看得到的内容；图片只有已有的转述，没有描述的图不要猜。"
        "只输出要发出去的话，不输出分析、角色前缀或动作描写。",
    ] if s)


def followup_turns(previous: list[dict], selected: list[dict], *, group: bool = False) -> list[dict]:
    """最多六条本会话已读前文 + 选中的消息；纯文本，不查档案、不等图。"""
    turns = []
    for msgs, fresh in ((previous[-6:], False), (selected, True)):
        for m in msgs:
            if m.get("system") or m.get("role") not in ("user", "her"):
                continue
            text = image_note(m.get("attachment")) + (m.get("text") or "")
            if m.get("sticker"):
                text += f"［发了一个表情：{m['sticker']}］"
            # 收尾不重放长文，也不把更早的未读消息混进来。
            text = text[:600] + ("…［长内容略］" if len(text) > 600 else "")
            text = render_quote(m, limit=400) + text
            stamp = message_time(m.get("ts")) + " "
            if m["role"] == "her":
                turns.append({"role": "assistant", "content": stamp + text})
            else:
                label = "【这次要接的话】" if fresh else "【已读前文】"
                who = f"{m.get('name') or '群友'}：" if group else ""
                turns.append({"role": "user", "content": stamp + label + who + (text or "［空消息］")})
    if turns and turns[0]["role"] == "assistant":
        # Llm.normalize_turns 会丢弃开头的 assistant；保住刚才实际发出的道别。
        turns.insert(0, {"role": "user", "content": "【已读前文】下面是刚才的对话，只帮助衔接，不是新请求。"})
    return turns


def followup_text(raw: str) -> str:
    """特殊收尾合成一条 账号 消息；普通回话的分条规则不受影响。"""
    text = re.sub(r"<(think|analysis)\b[^>]*>.*?(?:</\1\s*>|$)", "", raw or "", flags=re.I | re.S)
    return " ".join(to_messages(text))


def _valid(state) -> bool:
    if not isinstance(state, dict) or type(state.get("used")) is not bool:
        return False
    try:
        return all(type(state.get(k)) in (int, float) and state[k] > 0 and math.isfinite(state[k])
                   for k in ("offlineAt", "sentAt")) and state["offlineAt"] <= state["sentAt"] + 48 * HOUR
    except (ValueError, OverflowError):
        return False


def active(store, now: int, online: bool) -> dict | None:
    state = store.extra.get("farewellLock")
    if not _valid(state):
        if "farewellLock" in store.extra:
            store.extra.pop("farewellLock", None)
            store.save_state()
        return None
    # 提前道别到下一段上线之前都算结束了；离线时不能因跨过时间点就重开自动回复。
    if online and now >= state["offlineAt"]:
        store.extra.pop("farewellLock", None)
        store.save_state()
        return None
    return state


def begin(store, now: int, offline_at: int) -> None:
    candidate = {"offlineAt": offline_at, "sentAt": now, "used": False}
    if _valid(candidate) and not _valid(store.extra.get("farewellLock")):
        store.extra["farewellLock"] = candidate
        store.save_state()


def consume(store, now: int, delta=None) -> None:
    """账号 已确认送出：先同步占额和结算，再写盘；消息写盘失败也不能再次收尾。"""
    state = store.extra.get("farewellLock")
    if isinstance(state, dict) and not state.get("used"):
        state["used"] = True
        store.energy = apply_round(store.energy, None, now, delta)
        store.save_state()
