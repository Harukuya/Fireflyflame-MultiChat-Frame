"""离线状态通知：任务与休息分开计次，不启动聊天或把留言算作已回复。"""

from __future__ import annotations

import asyncio

from .jsutil import HOUR, MIN
from .llm import classify_exception, error_diagnostic
from .sanitize import to_messages

REQUEST_TIMEOUT_S = 20

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


async def short_reply(llm, system: str, turns: list[dict], *, valid, count, note, track) -> str | None:
    """一次有截止时间的短通知请求；不带工具，取消直接向上抛，不偷偷转成固定回复。"""
    if not valid() or llm.provider() is None:
        return None
    count("模型调用")
    task = asyncio.create_task(llm.chat_with_retry(system, turns, request_valid=valid))
    track(task)
    try:
        result = await asyncio.wait_for(task, REQUEST_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result = classify_exception(exc)
    if not valid():
        return None
    parts = to_messages(result.text) if result.ok else []
    if parts:
        return parts[0]
    count("模型失败")
    note("出错", f"离线短应答：{error_diagnostic(result)}；用固定状态通知", "warn")
    return None



def notice_context(store, now: int) -> str | None:
    """只给仍待正式补发的近期留言承接真实通知，不把通知当作已经答过留言。"""
    notice = store.extra.get("offlineNoticeText")
    if not isinstance(notice, dict) or not isinstance(notice.get("ts"), (int, float)):
        return None
    if not 0 <= now - notice["ts"] <= 48 * HOUR or not any(m.get("undelivered") for m in store.messages):
        return None
    return ("【你之前的离线短应答】\n" + str(notice.get("text") or "")
            + "\n这只是你当时的简短通知，留言里的事情还没有正式回答。")
