"""
她自己的情绪，所有会话共用，不是对某个人的好感或关系分数。
当前数值只供仪表盘；模型只看自然语言状态、报告自然感受。
"""

from __future__ import annotations

import math

from .jsutil import HOUR, day_of, now_ms

SCHEMA = 2
BASELINE = 80  # 用户定的回归基准保留；v0.2.35 追加调整档位后，平时落在「平稳」。
INITIAL = BASELINE
RECOVERY_PER_HOUR = 2
HOURLY_CHANGE_CAP = 8
DAILY_CHANGE_CAP = 24
EMOTIONS = ("uplifted", "steady", "down")


def _clamp(v: float) -> float:
    return max(0, min(100, v))


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def default_mood(now: int | None = None) -> dict:
    now = now_ms() if now is None else now
    return {"schema": SCHEMA, "value": BASELINE, "settledAt": now,
            "hour": math.floor(now / HOUR), "hourUsed": 0, "day": day_of(now), "dayUsed": 0}


def settle(m: dict | None, now: int | None = None) -> dict:
    """每完整经过一小时向平稳靠近；保留不足一小时的余数。旧关系心情不移植。"""
    now = now_ms() if now is None else now
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        return default_mood(now)
    try:
        value = float(m["value"])
        settled_at = int(m["settledAt"])
        if not math.isfinite(value):
            return default_mood(now)
        out = {**default_mood(now), **m, "value": _clamp(value)}
        hours = max(0, math.floor((now - settled_at) / HOUR))
        drift = min(hours * RECOVERY_PER_HOUR, abs(out["value"] - BASELINE))
        out["value"] += _sign(BASELINE - out["value"]) * drift
        out["settledAt"] = settled_at + hours * HOUR
        hour, day = math.floor(now / HOUR), day_of(now)
        out["hourUsed"] = max(0, float(out["hourUsed"])) if out["hour"] == hour else 0
        out["dayUsed"] = max(0, float(out["dayUsed"])) if out["day"] == day else 0
        out["hour"], out["day"] = hour, day
        return out
    except (KeyError, TypeError, ValueError, OverflowError):
        return default_mood(now)


def on_emotion(m: dict, emotion: str | None, meaningful: bool = False, now: int | None = None) -> dict:
    """真发出一轮以后，对最新共享状态结算一次；正负变化共同占用钟点／当日额度。"""
    now = now_ms() if now is None else now
    s = settle(m, now)
    if emotion not in ("uplifted", "down"):
        return s
    amount = min(4 if meaningful else 2, max(0, HOURLY_CHANGE_CAP - s["hourUsed"]),
                 max(0, DAILY_CHANGE_CAP - s["dayUsed"]))
    value = _clamp(s["value"] + (amount if emotion == "uplifted" else -amount))
    used = abs(value - s["value"])
    return {**s, "value": value, "hourUsed": s["hourUsed"] + used, "dayUsed": s["dayUsed"] + used}


def on_conversation(m: dict, depth: str, now: int | None = None) -> dict:
    """旧调用兼容：聊天发生本身不自动涨心情。"""
    return settle(m, now)


def on_neglect(m: dict, now: int | None = None) -> dict:
    """旧调用兼容：久未联系不会扣她自己的心情。"""
    return settle(m, now)


def band_of(value: float) -> str:
    if value >= 90:
        return "warm"
    if value >= 70:
        return "normal"
    if value >= 50:
        return "cool"
    return "distant"


def mood_instruction(value: float) -> str:
    """用自然情绪影响表达与话题倾向，不改变关系、人格或硬性回复长度。"""
    states = {
        "warm": "你现在心情轻快，比较放松。表达可以活泼些，合适时轻轻开玩笑；更容易想分享小事、表达喜欢、聊聊期待，也可以顺着对方的话展开一些。",
        "normal": "你现在心情平稳，像平常那样认真听、自然地接话。可以从容地说自己的看法，跟着眼前的话题聊，不必刻意制造热闹或情绪。",
        "cool": "你现在有一点低落，语气可以安静、柔和些，不必强装开心。可以接受关心，聊熟悉、安心的事情；对信任的人，也可以说一点真实感受。",
        "distant": "你现在心情很低，可能不太想主动展开，也可以在信任的人面前坦露脆弱、表达想被理解或陪着的心情。愿意倾诉时可以多说一些，也可以只是安静相处；不必强行振作。",
    }
    return (states[band_of(value)]
            + "\n**心情影响表达的情绪色彩和此刻想聊的方向，不直接规定回复长短，也不改变你的基本性格。**"
            + "\n这些只是倾向，不必每轮表现或说明心情；不要为了说明心情编造经历或原因，透露多深仍看你和对方的关系及眼前的话题。"
            + "这是你自己的情绪，不代表你和眼前这个人的关系变了；不要把它归咎于对方，也不要说出任何心情分数。")
