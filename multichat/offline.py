"""离线状态通知：任务与休息分开计次，不启动聊天或把留言算作已回复。"""

from __future__ import annotations

from .jsutil import MIN

REST_TEXT = "对方正在休息中"
MISSION_TEXT = "对方正在忙碌中"
TRAILBLAZER_TEXT = "我在忙，等空下来再回你。"


def kind(state) -> str | None:
    if state.online:
        return None
    return "mission" if state.activity == "mission" else "rest"


def segment_key(now: int, state_fn) -> str | None:
    """当前连续离线类型首次结束的分钟；任务↔休息即使没上线也分别计次。"""
    current = kind(state_fn(now))
    if current is None:
        return None
    t = now // MIN * MIN + MIN
    for _ in range(48 * 60):
        if kind(state_fn(t)) != current:
            return f"{current}:{t}"
        t += MIN
    return f"{current}:long"  # 测试替身或异常作息一直离线时也保持稳定，不逐分钟换键。


def matched(value, key: str | None) -> bool:
    """兼容原来按下一次上线分钟保存的通知记录，不清除已有去重状态。"""
    return key is not None and (value == key or (type(value) is int and str(value) == key.split(":", 1)[1]))


def text(options: dict, state_kind: str, trailblazer: bool = False) -> str:
    if state_kind == "mission" and trailblazer:
        return str(options.get("mission_ack_text") or TRAILBLAZER_TEXT)
    field, default = ("offline_text_mission", MISSION_TEXT) if state_kind == "mission" else ("offline_text_rest", REST_TEXT)
    value = options.get(field, default)
    return str(value) if value is not None else ""
